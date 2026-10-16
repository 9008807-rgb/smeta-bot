"""Telegram bot: lead magnet «Смета запуска бренда одежды» for the course «Бренд под ключ».

- /start [source]  -> sends the smeta (xlsx + pdf), asks the stage, logs the lead with its source
                      (deep link t.me/<bot>?start=clip01 lets us see which clip or ad brought the lead)
- stage buttons    -> beginners get the course path, working brands get the mastermind
- follow-ups       -> sales chain from texts.json (sent by the polling loop): d1 "what scares you" menu with
                      objection answers, d2 lesson clip from video/, d3 consultation, d5 course month 1,
                      d7 tariffs with prices, d10 goodbye. Brands that already sell get only d3
- «Консультация»   -> asks for a phone (contact button or typed), then a convenient time,
                      and forwards the lead with stage, objections and source to the admin chats
- /admin <code>    -> adds the chat to the lead recipients (code is in config.json); several admins allowed
- /admin_off       -> removes the chat from the recipients
- /stats           -> admins only: leads by source and stage
- /backup          -> admins only: sends state.json and leads.csv to the chat

No third-party packages: plain Bot API over urllib, long polling. State lives in state.json, leads in leads.csv.

Hosting (Bothost): the token comes from BOT_TOKEN, the admin code from ADMIN_CODE, and state/leads live in
DATA_DIR (/app/data, the only folder that survives a redeploy). Locally the same files sit next to bot.py.
On the first start with an empty DATA_DIR the bot restores state and leads from the file_ids in migrate.json
(files sent to the admin chat by migrate.py; a file_id is useless without the bot token).
"""
import csv, json, mimetypes, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
TOKEN = (os.environ.get("BOT_TOKEN") or (HERE / "token.txt").read_text(encoding="utf-8-sig")).strip()
_code_f = HERE / "admin_code.txt"
ADMIN_CODE = (os.environ.get("ADMIN_CODE") or CFG.get("admin_code")
              or (_code_f.read_text(encoding="utf-8-sig") if _code_f.exists() else "")).strip()
API = f"https://api.telegram.org/bot{TOKEN}/"
DATA = Path(os.environ.get("DATA_DIR") or HERE)
DATA.mkdir(parents=True, exist_ok=True)
STATE_F, LEADS_F = DATA / "state.json", DATA / "leads.csv"
TEXT = json.loads((HERE / "texts.json").read_text(encoding="utf-8"))
sys.stdout.reconfigure(encoding="utf-8")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def restore_from_telegram():
    """first start on a new host: pull state.json and leads.csv that migrate.py left in the admin chat"""
    mig = HERE / "migrate.json"
    if STATE_F.exists() or not mig.exists():
        return
    for name, fid in json.loads(mig.read_text(encoding="utf-8")).items():
        path = call("getFile", file_id=fid)["file_path"]
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=120) as r:
            (DATA / name).write_bytes(r.read())
        log("restored", name)


def load_state():
    if STATE_F.exists():
        return json.loads(STATE_F.read_text(encoding="utf-8"))
    return {"offset": 0, "users": {}, "file_ids": {}, "admin_chats": [c for c in [CFG.get("admin_chat")] if c]}


def save_state(s):
    tmp = STATE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_F)


def call(method, _http_timeout=70, **params):
    data = urllib.parse.urlencode({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    try:
        with urllib.request.urlopen(API + method, data=data, timeout=_http_timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        # keep Telegram's description, a bare "400 Bad Request" says nothing
        raise RuntimeError(f"{method}: {e.code} {e.read().decode('utf-8', 'replace')[:300]}") from None
    if not res.get("ok"):
        raise RuntimeError(res)
    return res["result"]


def send_file(chat, path, caption, state, kind="document", markup=None, meta=None):
    """upload once, then reuse Telegram file_id; kind is "document" or "video".
    meta = {"duration", "width", "height"}: without it Telegram shows a video as 0:00 with a wrong frame"""
    method = "sendVideo" if kind == "video" else "sendDocument"
    extra = {k: str(v) for k, v in (meta or {}).items()}
    if markup:
        extra["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    fid = state["file_ids"].get(path.name)
    if fid:
        return call(method, chat_id=chat, caption=caption, **{kind: fid}, **extra)
    b = uuid.uuid4().hex
    parts = []
    fields = {"chat_id": str(chat), "caption": caption, **extra}
    if kind == "video":
        fields["supports_streaming"] = "true"
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    fname = urllib.parse.quote(path.name)
    parts.append((f'--{b}\r\nContent-Disposition: form-data; name="{kind}"; filename="file{path.suffix}"; '
                  f"filename*=UTF-8''{fname}\r\nContent-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(API + method, data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        res = json.load(r)["result"]
    state["file_ids"][path.name] = res[kind]["file_id"]
    return res


def send_backup(chat, path, caption):
    """plain upload without the file_id cache: these files change every time"""
    state = {"file_ids": {}}
    return send_file(chat, path, caption, state)["document"]["file_id"]


def ack(cq):
    # a late answer on a slow link gets 400 "query is too old"; the reply itself must still go out
    try:
        call("answerCallbackQuery", callback_query_id=cq["id"])
    except Exception as e:
        log("ack failed", e)


def button_style(data):
    # grey default buttons read as plain text in a chat; coloured ones look tappable.
    # green = the step we want (consultation, its time slot), blue = everything else
    return "success" if data == "consult" or data.startswith("slot:") else "primary"


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "style": button_style(d),
                                  **({"url": d} if d.startswith("http") else {"callback_data": d})}
                                 for t, d in row] for row in rows]}


def log_lead(u, event, extra=""):
    new = not LEADS_F.exists()
    with open(LEADS_F, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["время", "событие", "chat_id", "имя", "username", "источник", "этап", "доп"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M"), event, u["id"], u.get("name", ""), u.get("username", ""),
                    u.get("source", ""), u.get("stage", ""), extra])


def admins(state):
    # several people can get leads; older state kept a single "admin_chat"
    lst = state.setdefault("admin_chats", [])
    old = state.pop("admin_chat", None)
    if old and old not in lst:
        lst.append(old)
    return lst


def notify_admin(state, text):
    for chat in admins(state):
        try:
            call("sendMessage", chat_id=chat, text=text)
        except Exception as e:
            log("admin notify failed", chat, e)


def on_start(msg, state):
    chat = msg["chat"]["id"]
    fr = msg.get("from", {})
    arg = msg.get("text", "").split(maxsplit=1)
    source = arg[1][:40] if len(arg) > 1 else "direct"
    u = state["users"].setdefault(str(chat), {"id": chat, "t0": time.time(), "sent": []})
    u.update(name=" ".join(x for x in (fr.get("first_name"), fr.get("last_name")) if x),
             username=fr.get("username", ""))
    u.setdefault("source", source)
    first = fr.get("first_name") or ""
    call("sendMessage", chat_id=chat, text=TEXT["hello"].format(name_sep=f", {first}" if first else ""))
    for p in sorted((HERE / "files").glob("*")):
        send_file(chat, p, TEXT["file_caption"], state)
    call("sendMessage", chat_id=chat, text=TEXT["ask_stage"],
         reply_markup=kb([[(t, "stage:" + k)] for k, t in TEXT["stages"].items()]))
    log_lead(u, "start")
    log("start", chat, source)


def on_stage(cq, state):
    chat = cq["message"]["chat"]["id"]
    key = cq["data"].split(":", 1)[1]
    u = state["users"].setdefault(str(chat), {"id": chat, "t0": time.time(), "sent": [], "source": "direct"})
    u["stage"] = key
    ack(cq)
    if key == "brand":
        call("sendMessage", chat_id=chat, text=TEXT["stage_brand"],
             reply_markup=kb([[("Мастермайнд для брендов", CFG["mastermind_url"])],
                              [("Хочу консультацию", "consult")]]))
    else:
        call("sendMessage", chat_id=chat, text=TEXT["stage_new"],
             reply_markup=kb([[("Telegram-канал Анны", CFG["channel_url"])],
                              [("Про курс «Бренд под ключ»", CFG["course_url"])]]))
    log_lead(u, "stage", key)


def user(state, chat):
    return state["users"].setdefault(str(chat), {"id": chat, "t0": time.time(), "sent": [], "source": "direct"})


def on_consult(chat, state):
    user(state, chat)["await_phone"] = True
    call("sendMessage", chat_id=chat, text=TEXT["ask_phone"],
         reply_markup={"keyboard": [[{"text": "📱 Отправить номер", "request_contact": True, "style": "success"}]],
                       "resize_keyboard": True, "one_time_keyboard": True})


def parse_phone(text):
    digits = "".join(c for c in text if c.isdigit())
    if not 10 <= len(digits) <= 12 or any(c.isalpha() for c in text):
        return None
    return ("+" if text.strip().startswith("+") else "") + digits


def on_contact(msg, state, phone=None):
    chat = msg["chat"]["id"]
    u = user(state, chat)
    phone = phone or msg["contact"].get("phone_number", "")
    u["phone"] = phone
    u.pop("await_phone", None)
    call("sendMessage", chat_id=chat, text=TEXT["ask_slot"], reply_markup=kb(
        [[(t, "slot:" + k)] for k, t in TEXT["slots"].items()]))
    log_lead(u, "consult", phone)
    notify_admin(state, f"Заявка на консультацию\n{u.get('name', '')} @{u.get('username', '')}\n{phone}\n"
                        f"Этап: {TEXT['stages'].get(u.get('stage', ''), '—')}\n"
                        f"Что пугает: {', '.join(TEXT['objections'][k]['button'] for k in u.get('obj', [])) or '—'}\n"
                        f"Источник: {u.get('source', '')}")


def on_slot(cq, state):
    chat = cq["message"]["chat"]["id"]
    u = user(state, chat)
    slot = TEXT["slots"].get(cq["data"].split(":", 1)[1], "")
    u["slot"] = slot
    ack(cq)
    call("sendMessage", chat_id=chat, text=TEXT["thanks_phone"], reply_markup={"remove_keyboard": True})
    log_lead(u, "slot", slot)
    notify_admin(state, f"{u.get('name', '')} @{u.get('username', '')} {u.get('phone', '')}: удобно {slot}")


def on_objection(cq, state):
    chat = cq["message"]["chat"]["id"]
    key = cq["data"].split(":", 1)[1]
    u = user(state, chat)
    if key not in u.setdefault("obj", []):
        u["obj"].append(key)
    ack(cq)
    call("sendMessage", chat_id=chat, text=TEXT["objections"][key]["text"],
         reply_markup=kb([[(t, d.format(**CFG))] for t, d in TEXT["objection_buttons"]]))
    log_lead(u, "objection", key)


def followups(state):
    now = time.time()
    for u in state["users"].values():
        if u.get("phone") or u.get("stopped"):
            continue
        for step in TEXT["followups"]:
            if step["id"] in u["sent"] or now - u["t0"] < step["after_hours"] * 3600:
                continue
            if step.get("only_stage") and u.get("stage", "") not in step["only_stage"]:
                u["sent"].append(step["id"])
                continue
            if step.get("objection_menu"):
                rows = [[(o["button"], "obj:" + k)] for k, o in TEXT["objections"].items()]
            else:
                rows = [[(t, d.format(**CFG))] for t, d in step["buttons"]]
            try:
                if step.get("video"):
                    send_file(u["id"], HERE / "video" / step["video"], step["text"], state, "video", kb(rows),
                              step.get("video_meta"))
                else:
                    call("sendMessage", chat_id=u["id"], text=step["text"], reply_markup=kb(rows))
            except Exception as e:
                if "blocked" in str(e) or "chat not found" in str(e):
                    u["stopped"] = True
                log("followup failed", u["id"], e)
            u["sent"].append(step["id"])
            log("followup", step["id"], u["id"])
            break


def stats(state):
    from collections import Counter
    us = state["users"].values()
    by_src = Counter(u.get("source", "?") for u in us)
    by_stage = Counter(u.get("stage", "не ответил") for u in us)
    by_obj = Counter(k for u in us for k in u.get("obj", []))
    consult = sum(1 for u in us if u.get("phone"))
    return ("Всего: %d, заявок на консультацию: %d\n\nИсточники:\n%s\n\nЭтапы:\n%s\n\nЧто пугает:\n%s" % (
        len(state["users"]), consult,
        "\n".join(f"{k}: {v}" for k, v in by_src.most_common()),
        "\n".join(f"{TEXT['stages'].get(k, k)}: {v}" for k, v in by_stage.most_common()),
        "\n".join(f"{TEXT['objections'][k]['button']}: {v}" for k, v in by_obj.most_common()) or "—"))


def handle(upd, state):
    if "callback_query" in upd:
        cq = upd["callback_query"]
        if cq["data"].startswith("stage:"):
            on_stage(cq, state)
        elif cq["data"].startswith("obj:"):
            on_objection(cq, state)
        elif cq["data"].startswith("slot:"):
            on_slot(cq, state)
        elif cq["data"] == "consult":
            ack(cq)
            on_consult(cq["message"]["chat"]["id"], state)
        return
    msg = upd.get("message")
    if not msg:
        return
    chat, text = msg["chat"]["id"], msg.get("text", "")
    waiting = state["users"].get(str(chat), {}).get("await_phone") and text and not text.startswith("/")
    if "contact" in msg:
        on_contact(msg, state)
    elif waiting and parse_phone(text):
        on_contact(msg, state, parse_phone(text))
    elif waiting:
        # ask once; after that the chat goes back to normal so a question is not answered with "bad phone" forever
        state["users"][str(chat)].pop("await_phone")
        call("sendMessage", chat_id=chat, text=TEXT["bad_phone"])
    elif text.startswith("/start"):
        on_start(msg, state)
    elif text.startswith("/admin_off"):
        if chat in admins(state):
            admins(state).remove(chat)
            call("sendMessage", chat_id=chat, text="Заявки сюда больше не приходят.")
    elif text.startswith("/admin"):
        if ADMIN_CODE and text.split()[-1] == ADMIN_CODE:
            if chat not in admins(state):
                admins(state).append(chat)
            call("sendMessage", chat_id=chat, text="Готово: заявки будут приходить сюда. "
                                                   f"Всего получателей: {len(admins(state))}. Отключить: /admin_off")
    elif text.startswith("/stats") and chat in admins(state):
        call("sendMessage", chat_id=chat, text=stats(state))
    elif text.startswith("/backup") and chat in admins(state):
        save_state(state)
        for p in (STATE_F, LEADS_F):
            if p.exists():
                send_backup(chat, p, "Резервная копия: " + time.strftime("%Y-%m-%d %H:%M"))
    elif text.startswith("/stop"):
        state["users"].setdefault(str(chat), {"id": chat, "t0": time.time(), "sent": []})["stopped"] = True
        call("sendMessage", chat_id=chat, text="Хорошо, больше не буду писать первой.")
    else:
        call("sendMessage", chat_id=chat, text=TEXT["fallback"],
             reply_markup=kb([[("Хочу консультацию", "consult")], [("Про курс", CFG["course_url"])]]))


def main():
    me = call("getMe")
    restore_from_telegram()
    state = load_state()
    log("bot @%s started, data in %s, %d users" % (me["username"], DATA, len(state["users"])))
    last_fu = 0
    while True:
        try:
            ups = call("getUpdates", _http_timeout=70, offset=state["offset"], timeout=50)
            for upd in ups:
                state["offset"] = upd["update_id"] + 1
                try:
                    handle(upd, state)
                except Exception as e:
                    log("handle error", repr(e)[:300])
            if time.time() - last_fu > 300:
                followups(state)
                last_fu = time.time()
            save_state(state)
        except Exception as e:
            log("loop error", repr(e)[:300])
            time.sleep(10)


if __name__ == "__main__":
    main()
