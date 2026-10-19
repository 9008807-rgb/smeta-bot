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
- channel autopost -> publishes posts.json (texts, clips, a poll) to the course channel at their Moscow time;
                      admins get a preview a day before and a link after. The bot must be a channel admin
                      allowed to post. /plan, /postnow <id>, /hold <id>, /unhold <id> for admins
- /broadcast       -> admins only: message (any kind) -> audience segment -> preview -> send to bot subscribers
                      in chunks from the main loop (queue kept in state), report at the end; /cancel aborts
- Instagram        -> instagram.py: reels and carousels from ig_posts.json at their Moscow time, private replies
                      to comment keywords (СМЕТА, КУРС). /igtoken <token> connects the account, /ig shows the plan;
                      /postnow, /hold, /unhold take ig.. ids too

No third-party packages: plain Bot API over urllib, long polling. State lives in state.json, leads in leads.csv.

Hosting (Bothost): the token comes from BOT_TOKEN, the admin code from ADMIN_CODE, and state/leads live in
DATA_DIR (/app/data, the only folder that survives a redeploy). Locally the same files sit next to bot.py.
On the first start with an empty DATA_DIR the bot restores state and leads from the file_ids in migrate.json
(files sent to the admin chat by migrate.py; a file_id is useless without the bot token).
"""
import csv, json, mimetypes, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # python -I leaves the script folder out of sys.path
import instagram as ig  # noqa: E402

VERSION = "v10 instagram"  # shows in the start log line: tells which code the host really runs
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
_posts_f = HERE / "posts.json"
POSTS = json.loads(_posts_f.read_text(encoding="utf-8")) if _posts_f.exists() else {"channel": "", "posts": []}
MSK = 3 * 3600          # Moscow has no daylight saving; the host clock is UTC
LATE_LIMIT = 12 * 3600  # a post more than 12 h late (bot was down) is reported, not published out of context
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


def send_file(chat, path, caption, state, kind="document", markup=None, meta=None, parse_mode=None):
    """upload once, then reuse Telegram file_id; kind is "document" or "video".
    meta = {"duration", "width", "height"}: without it Telegram shows a video as 0:00 with a wrong frame"""
    method = "sendVideo" if kind == "video" else "sendDocument"
    extra = {k: str(v) for k, v in (meta or {}).items()}
    if parse_mode:
        extra["parse_mode"] = parse_mode
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
    # Telegram takes the plain filename and ignores filename*: a placeholder there showed up as "file.pdf"
    fname = path.name.replace('"', "'")
    parts.append((f'--{b}\r\nContent-Disposition: form-data; name="{kind}"; filename="{fname}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
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


def is_ours(chat_id, name=""):
    """our own team accounts (config "ours"): they test the bot and must not count as leads"""
    ours = CFG.get("ours", {})
    name = " ".join(str(name).replace("ё", "е").replace("Ё", "Е").split()).casefold()
    return (int(chat_id) in ours.get("ids", [])
            or any(n.replace("ё", "е").casefold() in name for n in ours.get("names", []) if name))


def purge_ours(state):
    """drop our team accounts from users and leads.csv (run at start; new rows are not written at all)"""
    gone = [k for k, u in state["users"].items() if is_ours(u["id"], u.get("name", ""))]
    for k in gone:
        state["users"].pop(k)
    if LEADS_F.exists():
        with open(LEADS_F, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f, delimiter=";"))
        keep = rows[:1] + [r for r in rows[1:] if len(r) < 4 or not is_ours(r[2], r[3])]
        if len(keep) != len(rows):
            with open(LEADS_F, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=";").writerows(keep)
        gone_rows = len(rows) - len(keep)
    else:
        gone_rows = 0
    if gone or gone_rows:
        log("purged our contacts: %d users, %d lead rows" % (len(gone), gone_rows))


def log_lead(u, event, extra=""):
    if is_ours(u["id"], u.get("name", "")):
        return
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
    # owners listed in config.json are admins even if state.json is lost or reset on the host
    # (chat ids are not secret; /admin <code> stays for adding someone new)
    for c in CFG.get("admins", []):
        if c not in lst and c not in state.get("admins_off", []):
            lst.append(c)
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
    for p in sorted((HERE / "files").glob("*"), key=lambda p: (p.suffix != ".pdf", p.name)):  # PDF first
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
    us = [u for u in state["users"].values() if not is_ours(u["id"], u.get("name", ""))]
    by_src = Counter(u.get("source", "?") for u in us)
    by_stage = Counter(u.get("stage", "не ответил") for u in us)
    by_obj = Counter(k for u in us for k in u.get("obj", []))
    consult = sum(1 for u in us if u.get("phone"))
    return ("Всего: %d, заявок на консультацию: %d\n\nИсточники:\n%s\n\nЭтапы:\n%s\n\nЧто пугает:\n%s" % (
        len(us), consult,
        "\n".join(f"{k}: {v}" for k, v in by_src.most_common()),
        "\n".join(f"{TEXT['stages'].get(k, k)}: {v}" for k, v in by_stage.most_common()),
        "\n".join(f"{TEXT['objections'][k]['button']}: {v}" for k, v in by_obj.most_common()) or "—"))


def post_time(p):
    """posts.json time is Moscow wall clock 'YYYY-MM-DD HH:MM' -> unix time"""
    import calendar
    return calendar.timegm(time.strptime(p["at"], "%Y-%m-%d %H:%M")) - MSK


def channels():
    """[{"chat": "@name", "tag": "tg"}]; the tag replaces start=tg / utm_medium=channel in the links,
    so /stats and the site analytics show which channel brought the lead"""
    return POSTS.get("channels") or [{"chat": POSTS["channel"], "tag": "tg"}]


def channel_link(chat, msg_id):
    return f"https://t.me/{chat.lstrip('@')}/{msg_id}" if str(chat).startswith("@") else str(msg_id)


def retag(s, tag):
    return s.replace("start=tg", "start=" + tag).replace("utm_medium=channel", "utm_medium=" + tag)


def publish(p, state, ch=None, tag="tg"):
    """send one scheduled post to a channel, return the message id"""
    ch = ch or channels()[0]["chat"]
    text = retag(p.get("text", ""), tag)
    markup = kb([[(t, retag(u, tag))] for t, u in p.get("buttons", [])]) if p.get("buttons") else None
    if p.get("poll"):
        r = call("sendPoll", chat_id=ch, question=p["poll"]["question"],
                 options=[{"text": o} for o in p["poll"]["options"]], is_anonymous="true")
    elif p.get("video_file_id"):
        # clips live in Telegram (uploaded once by deploy/build_posts.py), not in the repo
        extra = {"reply_markup": markup} if markup else {}
        r = call("sendVideo", chat_id=ch, video=p["video_file_id"], caption=text, parse_mode="HTML",
                 supports_streaming="true", **{k: str(v) for k, v in p.get("video_meta", {}).items()}, **extra)
    elif p.get("video"):
        r = send_file(ch, HERE / "video" / p["video"], text, state, "video", markup, p.get("video_meta"),
                      parse_mode="HTML")
    else:
        extra = {"reply_markup": markup} if markup else {}
        r = call("sendMessage", chat_id=ch, text=text, parse_mode="HTML",
                 link_preview_options={"is_disabled": True}, **extra)
    return r["message_id"]


def is_held(p, state):
    return (p.get("hold") and p["id"] not in state.get("unheld", [])) or p["id"] in state.get("held", [])


def channel_posts(state, now=None):
    """publish what is due in every channel, warn admins a day ahead, report failures once per post and channel"""
    now = now or time.time()
    posted = state.setdefault("posted", {})          # "p01@anyazheltakova" -> message id or "missed"
    previewed, failed = state.setdefault("previewed", []), state.setdefault("post_failed", [])
    retry = state.setdefault("post_retry", {})
    names = ", ".join(c["chat"] for c in channels())
    for p in POSTS["posts"]:
        pid, t = p["id"], post_time(p)
        on_hold = is_held(p, state)
        if t - now <= 24 * 3600 and pid not in previewed and now < t:
            previewed.append(pid)
            note = ("⚠️ Пост на паузе: нужны данные от Анны. Опубликовать: /unhold " + pid) if on_hold else \
                   ("Снять с публикации: /hold " + pid)
            notify_admin(state, f"Завтра в {names}, {p['at']} МСК: {p['title']}\n\n"
                                f"{strip_html(p.get('text', ''))[:600]}\n\n{note}")
        if now < t or on_hold:
            continue
        for c in channels():
            key = f"{pid}{c['chat']}"
            if key in posted:
                continue
            if now - t > LATE_LIMIT:
                posted[key] = "missed"
                notify_admin(state, f"Пост {pid} «{p['title']}» не вышел вовремя в {c['chat']} ({p['at']} МСК) "
                                    f"и пропущен. Опубликовать сейчас: /postnow {pid}")
                continue
            if now - retry.get(key, 0) < 600:  # a failing post is retried every 10 minutes, not every poll
                continue
            retry[key] = now
            try:
                mid = publish(p, state, c["chat"], c["tag"])
                posted[key] = mid
                log("channel post", key, mid)
                notify_admin(state, f"Опубликовано в {c['chat']}: {p['title']}\n{channel_link(c['chat'], mid)}")
            except Exception as e:
                log("channel post failed", key, repr(e)[:300])
                if key not in failed:
                    failed.append(key)
                    notify_admin(state, f"Не получилось опубликовать {pid} «{p['title']}» в {c['chat']}: "
                                        f"{str(e)[:300]}\nПроверьте, что бот — администратор этого канала с правом "
                                        "публиковать сообщения. Бот будет пробовать снова каждые 10 минут.")


def strip_html(t):
    import html, re
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def plan(state):
    posted, chans = state.get("posted", {}), channels()
    rows = []
    for p in POSTS["posts"]:
        if all(f"{p['id']}{c['chat']}" in posted for c in chans):
            continue
        mark = "⏸ " if is_held(p, state) else ""
        rows.append(f"{p['id']}  {p['at'][5:]}  {mark}{p['title'][:45]}")
    done = ", ".join(f"{c['chat']}: {sum(1 for k, v in posted.items() if k.endswith(c['chat']) and v != 'missed')}"
                     for c in chans)
    return (f"Опубликовано — {done}. Впереди {len(rows)}.\n⏸ — на паузе.\n\n"
            + "\n".join(rows[:15]) + ("\n…" if len(rows) > 15 else "")
            + "\n\n/postnow p01 — опубликовать сейчас, /hold p01 — пауза, /unhold p01 — снять паузу")


ADMIN_MENU = [("start", "Получить смету"), ("plan", "Расписание постов в канал"),
              ("ig", "Instagram: расписание и ответы"),
              ("broadcast", "Рассылка подписчикам бота"), ("stats", "Статистика заявок"),
              ("backup", "Прислать базу и заявки"),
              ("hold", "Пауза поста: /hold p05"), ("unhold", "Снять паузу: /unhold p05"),
              ("postnow", "Опубликовать сейчас: /postnow p05"), ("stop", "Не присылать сообщения")]


def set_admin_menu(state):
    """the "/" menu next to the input field: admins see their commands, everyone else keeps /start and /stop"""
    for chat in admins(state):
        try:
            call("setMyCommands", commands=[{"command": c, "description": d} for c, d in ADMIN_MENU],
                 scope={"type": "chat", "chat_id": chat})
        except Exception as e:
            log("admin menu failed", chat, e)


def admin_post_cmd(text, chat, state):
    cmd, _, pid = text.partition(" ")
    pid = pid.strip()
    p = next((x for x in POSTS["posts"] + ig.IG["posts"] if x["id"] == pid), None)
    if p and pid.startswith("ig") and cmd == "/postnow":
        try:
            reply = ig.publish_now(p, state)
        except Exception as e:
            reply = f"Instagram: не получилось — {str(e)[:300]}"
        call("sendMessage", chat_id=chat, text=reply)
        return
    if not p:
        # picked from the menu without a number: explain and show what can be picked
        call("sendMessage", chat_id=chat, text=f"Напишите команду с номером поста, например: {cmd} p05\n\n" + plan(state))
        return
    held, unheld = state.setdefault("held", []), state.setdefault("unheld", [])
    if cmd == "/hold":
        held.append(pid) if pid not in held else None
        unheld.remove(pid) if pid in unheld else None
        call("sendMessage", chat_id=chat, text=f"{pid} на паузе, не выйдет, пока не пришлёте /unhold {pid}")
    elif cmd == "/unhold":
        held.remove(pid) if pid in held else None
        unheld.append(pid) if pid not in unheld else None
        call("sendMessage", chat_id=chat, text=f"{pid} снят с паузы и выйдет {p['at']} МСК")
    elif cmd == "/postnow":
        links = []
        for c in channels():
            try:
                mid = publish(p, state, c["chat"], c["tag"])
                state.setdefault("posted", {})[f"{pid}{c['chat']}"] = mid
                links.append(channel_link(c["chat"], mid))
            except Exception as e:
                links.append(f"{c['chat']}: не получилось — {str(e)[:200]}")
        call("sendMessage", chat_id=chat, text="Опубликовано:\n" + "\n".join(links))


# --- broadcast to everyone who started the bot (Telegram never lets a bot write to channel subscribers) ---
SEGMENTS = [("all", "Всем"), ("new", "Новичкам: мечта, идея, образцы"), ("brand", "У кого бренд уже продаёт"),
            ("nophone", "Кто ещё не оставил заявку"), ("tg", "Пришли из канала Анны"),
            ("m4ch", "Пришли из канала производства"), ("vk", "Пришли из рекламы VK")]
BC_CHUNK = 25  # messages per loop turn; with getUpdates timeout 0 that is ~20/s, under Telegram's 30/s limit


def segment(state, key):
    out = []
    for u in state["users"].values():
        if u.get("stopped") or is_ours(u["id"], u.get("name", "")):
            continue
        src, stage = u.get("source", ""), u.get("stage", "")
        if (key == "all" or key == "new" and stage in ("dream", "idea", "samples") or key == "brand" and stage == "brand"
                or key == "nophone" and not u.get("phone") or key == "tg" and src == "tg"
                or key == "m4ch" and src == "m4ch" or key == "vk" and src.startswith("vk")):
            out.append(u["id"])
    return out


def bc_start(chat, state):
    state.setdefault("bc_draft", {})[str(chat)] = {"step": "content"}
    call("sendMessage", chat_id=chat, text="Пришлите сообщение для рассылки: текст, фото или видео с подписью. "
                                           "Оно уйдёт подписчикам бота в точности как вы его пришлёте.\n\nОтмена: /cancel")


def bc_content(msg, state):
    chat = msg["chat"]["id"]
    state["bc_draft"][str(chat)] = {"step": "segment", "msg": msg["message_id"]}
    rows = [[(f"{t} — {len(segment(state, k))}", "bc:seg:" + k)] for k, t in SEGMENTS]
    call("sendMessage", chat_id=chat, text="Кому отправить? Цифра — сколько человек получит.",
         reply_markup=kb(rows + [[("Отмена", "bc:cancel")]]))


def bc_callback(cq, state):
    chat, data = cq["message"]["chat"]["id"], cq["data"]
    ack(cq)
    draft = state.setdefault("bc_draft", {}).get(str(chat))
    if chat not in admins(state):
        return
    if data == "bc:cancel" or not draft:
        state["bc_draft"].pop(str(chat), None)
        call("sendMessage", chat_id=chat, text="Рассылка отменена.")
        return
    if data.startswith("bc:seg:"):
        key = data.split(":", 2)[2]
        n = len(segment(state, key))
        draft.update(step="confirm", seg=key)
        call("sendMessage", chat_id=chat, text="Превью — так увидят подписчики:")
        call("copyMessage", chat_id=chat, from_chat_id=chat, message_id=draft["msg"])
        call("sendMessage", chat_id=chat, text=f"Аудитория: {dict(SEGMENTS)[key]}. Получат {n} чел.",
             reply_markup=kb([[(f"Отправить {n} чел.", "bc:send")], [("Отмена", "bc:cancel")]]))
    elif data == "bc:send" and draft.get("step") == "confirm":
        ids = segment(state, draft["seg"])
        state.setdefault("bc_queue", []).append({"admin": chat, "from": chat, "msg": draft["msg"], "todo": ids,
                                                 "ok": 0, "blocked": 0, "failed": 0, "total": len(ids)})
        state["bc_draft"].pop(str(chat), None)
        call("sendMessage", chat_id=chat, text=f"Отправляю {len(ids)} чел. Пришлю отчёт, когда закончу.")


def bc_tick(state):
    """send the next chunk of the current broadcast; the queue lives in state, so a restart resumes it"""
    q = state.get("bc_queue") or []
    if not q:
        return
    job = q[0]
    for _ in range(min(BC_CHUNK, len(job["todo"]))):
        uid = job["todo"].pop(0)
        try:
            call("copyMessage", chat_id=uid, from_chat_id=job["from"], message_id=job["msg"])
            job["ok"] += 1
        except Exception as e:
            if "blocked" in str(e) or "chat not found" in str(e) or "deactivated" in str(e):
                job["blocked"] += 1
                state["users"].get(str(uid), {})["stopped"] = True
            elif "429" in str(e):           # too fast: put the user back and wait for the next turn
                job["todo"].insert(0, uid)
                time.sleep(5)
                break
            else:
                job["failed"] += 1
                log("broadcast failed", uid, str(e)[:200])
        time.sleep(0.05)
    if not job["todo"]:
        q.pop(0)
        call("sendMessage", chat_id=job["admin"], text=f"Рассылка готова: доставлено {job['ok']} из {job['total']}, "
                                                       f"заблокировали бота {job['blocked']}, ошибок {job['failed']}.")


def handle(upd, state):
    if "callback_query" in upd:
        cq = upd["callback_query"]
        if cq["data"].startswith("bc:"):
            bc_callback(cq, state)
        elif cq["data"].startswith("stage:"):
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
    draft = state.get("bc_draft", {}).get(str(chat))
    if text.startswith("/cancel") and draft:
        state["bc_draft"].pop(str(chat))
        call("sendMessage", chat_id=chat, text="Рассылка отменена.")
    elif draft and draft["step"] == "content" and not text.startswith("/"):
        bc_content(msg, state)
    elif text.startswith("/broadcast") and chat in admins(state):
        bc_start(chat, state)
    elif "contact" in msg:
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
            state.setdefault("admins_off", []).append(chat)
            call("sendMessage", chat_id=chat, text="Заявки сюда больше не приходят.")
    elif text.startswith("/admin"):
        if ADMIN_CODE and text.split()[-1] == ADMIN_CODE:
            if chat in state.get("admins_off", []):
                state["admins_off"].remove(chat)
            if chat not in admins(state):
                admins(state).append(chat)
            set_admin_menu(state)
            call("sendMessage", chat_id=chat, text="Готово: заявки будут приходить сюда. "
                                                   f"Всего получателей: {len(admins(state))}. Отключить: /admin_off")
    elif text.startswith("/stats") and chat in admins(state):
        call("sendMessage", chat_id=chat, text=stats(state))
    elif text.startswith("/plan") and chat in admins(state):
        call("sendMessage", chat_id=chat, text=plan(state))
    elif text.startswith("/igtoken") and chat in admins(state):
        # the token is a password to the account: take it and remove the message from the chat history
        try:
            call("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        except Exception as e:
            log("igtoken delete failed", e)
        tok = text.partition(" ")[2].strip()
        try:
            name = ig.set_token(state, tok)
            reply = (f"Instagram подключён: @{name}. Токен сохранён, сообщение с ним я удалил. "
                     "Расписание: /ig")
        except Exception as e:
            reply = f"Токен не подошёл: {str(e)[:300]}"
        call("sendMessage", chat_id=chat, text=reply)
    elif text.split(" ")[0] == "/ig" and chat in admins(state):
        call("sendMessage", chat_id=chat, text=ig.plan(state))
    elif text.split(" ")[0] in ("/postnow", "/hold", "/unhold") and chat in admins(state):
        admin_post_cmd(text, chat, state)
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
    purge_ours(state)
    save_state(state)
    ig.init(log, notify_admin)
    set_admin_menu(state)
    log("bot @%s started (%s), data in %s, %d users" % (me["username"], VERSION, DATA, len(state["users"])))
    last_fu = 0
    while True:
        try:
            busy = bool(state.get("bc_queue"))  # while a broadcast runs, do not wait 50 s for updates
            ups = call("getUpdates", _http_timeout=70, offset=state["offset"], timeout=0 if busy else 50)
            for upd in ups:
                state["offset"] = upd["update_id"] + 1
                try:
                    handle(upd, state)
                except Exception as e:
                    log("handle error", repr(e)[:300])
            if time.time() - last_fu > 300:
                followups(state)
                last_fu = time.time()
            channel_posts(state)
            ig.tick(state)
            bc_tick(state)
            save_state(state)
        except Exception as e:
            log("loop error", repr(e)[:300])
            time.sleep(10)


if __name__ == "__main__":
    main()
