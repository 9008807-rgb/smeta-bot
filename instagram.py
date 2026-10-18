"""Instagram autoposting and comment keywords for the course account (Instagram API with Instagram Login).

- ig_posts.json: reels and carousels with their Moscow time. Media sit in a public GitHub repo (media_base):
  Instagram downloads image_url / video_url itself, it never takes the file bytes from us.
- Publishing is two-step and slow for video: create a container, wait until Instagram reports FINISHED,
  then media_publish. tick() does one step per call from the bot's main loop, nothing blocks polling.
- Comments: the bot reads comments under the account's posts of the last 7 days and answers a keyword
  (СМЕТА, КУРС) with a private message plus a short public reply. Instagram allows one private reply per
  comment and only within 7 days of it.
- Token: an admin sends /igtoken <token> to the bot (never kept in the public repo). It is a long-lived
  Instagram user token (60 days); the bot refreshes it every 7 days, so it never expires while the bot runs.

No third-party packages, same as bot.py.
"""
import calendar, json, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

GRAPH = "https://graph.instagram.com/v23.0/"
HERE = Path(__file__).parent
_f = HERE / "ig_posts.json"
IG = json.loads(_f.read_text(encoding="utf-8")) if _f.exists() else {"posts": [], "keywords": {}}
MSK = 3 * 3600
LATE_LIMIT = 12 * 3600
REFRESH_EVERY = 7 * 86400
COMMENTS_EVERY = 180
WAIT_LIMIT = 3600          # a container that is not FINISHED after an hour is reported and dropped
log = notify = None        # set by init()


def init(log_fn, notify_fn):
    global log, notify
    log, notify = log_fn, notify_fn


def http(method, path, params=None, body=None, token=None):
    """one Graph API call; errors keep Instagram's message (error.message, error_user_msg)"""
    params = dict(params or {})
    if token:
        params["access_token"] = token
    url = (path if path.startswith("http") else GRAPH + path)
    data = None
    headers = {}
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    elif body is not None:
        url += "?" + urllib.parse.urlencode(params)
        data, headers = json.dumps(body, ensure_ascii=False).encode(), {"Content-Type": "application/json"}
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers),
                                    timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)["error"]
            msg = err.get("error_user_msg") or err.get("message") or raw
        except Exception:
            msg = raw
        raise RuntimeError(f"Instagram {e.code}: {msg[:300]}") from None


def st(state):
    return state.setdefault("ig", {"posted": {}, "pending": {}, "replied": [], "previewed": [], "failed": []})


def post_time(p):
    return calendar.timegm(time.strptime(p["at"], "%Y-%m-%d %H:%M")) - MSK


def url(name):
    return IG["media_base"].rstrip("/") + "/" + urllib.parse.quote(name)


# --- token ---------------------------------------------------------------------------------------------
def set_token(state, token):
    """check the token, remember the account; returns the username"""
    me = http("GET", "me", {"fields": "user_id,username"}, token=token)
    s = st(state)
    s.update(token=token, user_id=str(me["user_id"]), username=me["username"], token_at=time.time())
    s.pop("token_dead", None)
    return me["username"]


def refresh(state, now):
    s = st(state)
    if not s.get("token") or now - s.get("token_at", 0) < REFRESH_EVERY:
        return
    try:
        r = http("GET", "https://graph.instagram.com/refresh_access_token",
                 {"grant_type": "ig_refresh_token"}, token=s["token"])
        s.update(token=r["access_token"], token_at=now)
        log("ig token refreshed")
    except Exception as e:
        s["token_at"] = now - REFRESH_EVERY + 3600   # try again in an hour
        log("ig refresh failed", e)
        if not s.get("token_dead"):
            s["token_dead"] = True
            notify(state, f"Instagram: не получилось продлить токен ({str(e)[:200]}). Если публикации "
                          "перестанут выходить, пришлите новый: /igtoken <токен>")


# --- publishing ----------------------------------------------------------------------------------------
def caption(p):
    return p["caption"][:2200]


def start(p, state):
    """create the container(s); returns the container id to wait for"""
    s = st(state)
    tok, uid = s["token"], s["user_id"]
    if p["kind"] == "reel":
        params = {"media_type": "REELS", "video_url": url(p["video"]), "caption": caption(p), "share_to_feed": "true"}
        if p.get("thumb_offset") is not None:
            params["thumb_offset"] = str(p["thumb_offset"])
        return http("POST", f"{uid}/media", params, token=tok)["id"]
    if p["kind"] == "carousel":
        kids = [http("POST", f"{uid}/media", {"image_url": url(i), "is_carousel_item": "true"}, token=tok)["id"]
                for i in p["images"]]
        return http("POST", f"{uid}/media", {"media_type": "CAROUSEL", "children": ",".join(kids),
                                             "caption": caption(p)}, token=tok)["id"]
    if p["kind"] == "image":
        return http("POST", f"{uid}/media", {"image_url": url(p["image"]), "caption": caption(p)}, token=tok)["id"]
    raise ValueError(p["kind"])


def finish(pid, job, state, now):
    """check a waiting container; publish when ready. True when the job is over (published or failed)"""
    s = st(state)
    tok, uid = s["token"], s["user_id"]
    status = http("GET", job["container"], {"fields": "status_code,status"}, token=tok)
    code = status.get("status_code")
    if code == "FINISHED":
        mid = http("POST", f"{uid}/media_publish", {"creation_id": job["container"]}, token=tok)["id"]
        link = http("GET", mid, {"fields": "permalink"}, token=tok).get("permalink", "")
        s["posted"][pid] = mid
        log("ig posted", pid, mid)
        notify(state, f"Опубликовано в Instagram: {job['title']}\n{link}")
        return True
    if code in ("ERROR", "EXPIRED") or now - job["t"] > WAIT_LIMIT:
        fail(state, pid, job["title"], f"{code}: {status.get('status', '')}"[:300])
        return True
    return False


def fail(state, pid, title, err):
    s = st(state)
    log("ig post failed", pid, err)
    if pid not in s["failed"]:
        s["failed"].append(pid)
        notify(state, f"Instagram: не получилось опубликовать {pid} «{title}»: {err}\n"
                      f"Повторить: /postnow {pid}")


def is_held(p, state):
    return (p.get("hold") and p["id"] not in state.get("unheld", [])) or p["id"] in state.get("held", [])


def publish_now(p, state):
    """/postnow for an Instagram post: start it right away, the main loop finishes it"""
    s = st(state)
    if not s.get("token"):
        return "Instagram не подключён: пришлите /igtoken <токен>"
    s["posted"].pop(p["id"], None)
    if p["id"] in s["failed"]:
        s["failed"].remove(p["id"])
    s["pending"][p["id"]] = {"container": start(p, state), "t": time.time(), "title": p["title"]}
    return f"{p['id']} отправлен в Instagram. Видео обрабатывается несколько минут, ссылку пришлю."


def publish_due(state, now):
    s = st(state)
    for pid, job in list(s["pending"].items()):
        try:
            if finish(pid, job, state, now):
                s["pending"].pop(pid)
        except Exception as e:
            s["pending"].pop(pid)
            fail(state, pid, job["title"], str(e))
    for p in IG["posts"]:
        pid, t = p["id"], post_time(p)
        if t - now <= 24 * 3600 and now < t and pid not in s["previewed"]:
            s["previewed"].append(pid)
            note = f"⚠️ На паузе. Опубликовать: /unhold {pid}" if is_held(p, state) else f"Снять: /hold {pid}"
            notify(state, f"Завтра в Instagram, {p['at']} МСК: {p['title']}\n\n{p['caption'][:500]}\n\n{note}")
        if now < t or is_held(p, state) or pid in s["posted"] or pid in s["pending"] or pid in s["failed"]:
            continue
        if now - t > LATE_LIMIT:
            s["posted"][pid] = "missed"
            notify(state, f"Instagram: {pid} «{p['title']}» не вышел вовремя ({p['at']} МСК) и пропущен. "
                          f"Опубликовать сейчас: /postnow {pid}")
            continue
        if len(s["pending"]) >= 2:      # one at a time is enough; never flood the publishing limit
            break
        try:
            s["pending"][pid] = {"container": start(p, state), "t": now, "title": p["title"]}
            log("ig container", pid, s["pending"][pid]["container"])
        except Exception as e:
            fail(state, pid, p["title"], str(e))


# --- comment keywords ----------------------------------------------------------------------------------
def keyword(text):
    """'смета', 'Смету!', 'хочу СМЕТУ' -> 'смета': a word starting with the keyword's stem"""
    words = re.findall(r"[a-zа-яё]+", text.lower().replace("ё", "е"))
    for k, kw in IG.get("keywords", {}).items():
        stem = kw.get("stem", k)
        if any(w.startswith(stem) for w in words):
            return k
    return None


def comments(state, now):
    s = st(state)
    if now - s.get("comments_at", 0) < COMMENTS_EVERY:
        return
    s["comments_at"] = now
    tok, uid, me = s["token"], s["user_id"], s.get("username")
    replied = s["replied"]
    media = http("GET", f"{uid}/media", {"fields": "id,timestamp", "limit": "20"}, token=tok).get("data", [])
    for m in media:
        ts = calendar.timegm(time.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
        if now - ts > 7 * 86400:
            continue
        cs = http("GET", f"{m['id']}/comments", {"fields": "id,text,username,timestamp", "limit": "50"},
                  token=tok).get("data", [])
        for c in cs:
            if c["id"] in replied or c.get("username") == me:
                continue
            k = keyword(c.get("text", ""))
            if not k:
                continue
            replied.append(c["id"])
            kw = IG["keywords"][k]
            try:
                http("POST", f"{uid}/messages", body={"recipient": {"comment_id": c["id"]},
                                                      "message": {"text": kw["dm"]}}, token=tok)
                if kw.get("public"):
                    http("POST", f"{c['id']}/replies", {"message": kw["public"]}, token=tok)
                log("ig keyword", k, c.get("username"))
                state.setdefault("ig_leads", []).append({"t": now, "user": c.get("username"), "kw": k})
            except Exception as e:
                log("ig reply failed", c["id"], e)
    del replied[:-2000]


def tick(state, now=None):
    now = now or time.time()
    s = st(state)
    if not s.get("token"):
        return
    refresh(state, now)
    try:
        publish_due(state, now)
    except Exception as e:
        log("ig publish error", repr(e)[:300])
    try:
        comments(state, now)
    except Exception as e:
        log("ig comments error", repr(e)[:300])


def plan(state):
    s = st(state)
    head = (f"Instagram: @{s['username']}" if s.get("token") else "Instagram не подключён: /igtoken <токен>")
    rows = []
    for p in IG["posts"]:
        if p["id"] in s["posted"]:
            continue
        mark = "⏸ " if is_held(p, state) else ("⏳ " if p["id"] in s["pending"] else "")
        kind = "рилс" if p["kind"] == "reel" else "карусель"
        rows.append(f"{p['id']}  {p['at'][5:]}  {mark}{kind}: {p['title'][:38]}")
    done = sum(1 for v in s["posted"].values() if v != "missed")
    from collections import Counter
    kw = Counter(x["kw"] for x in state.get("ig_leads", []))
    return (f"{head}\nОпубликовано {done}, впереди {len(rows)}. "
            f"Ответов на комментарии: {', '.join(f'{k} {v}' for k, v in kw.items()) or '0'}.\n\n"
            + "\n".join(rows[:15]) + ("\n…" if len(rows) > 15 else "")
            + "\n\n/postnow ig05 — опубликовать сейчас, /hold ig05 — пауза, /unhold ig05 — снять паузу")
