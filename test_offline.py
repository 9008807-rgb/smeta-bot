"""Offline test: fake Telegram API, run the main handlers and follow-ups."""
import importlib.util, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
spec = importlib.util.spec_from_file_location("bot", Path(__file__).parent / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.STATE_F = Path(__file__).parent / "test_state.json"
bot.LEADS_F = Path(__file__).parent / "test_leads.csv"
for f in (bot.STATE_F, bot.LEADS_F):
    f.unlink(missing_ok=True)
sent = []
bot.call = lambda m, _http_timeout=70, **p: sent.append((m, p)) or {"document": {"file_id": "X"}}
def fake_send(chat, path, cap, st, kind="document", markup=None, meta=None, parse_mode=None):
    assert path.exists(), path
    sent.append(("send_" + kind, {"chat_id": chat, "file": path.name, "markup": markup}))
    return {kind: {"file_id": "X"}, "message_id": 700}
bot.send_file = fake_send
owners = bot.CFG.get("admins", [])
assert owners, "config.json must list the owners' chat ids"
assert bot.admins({"users": {}}) == owners             # host lost state.json: owners are still admins
bot.CFG["admins"] = []                                 # the rest of the test registers admins by code
st = bot.load_state()
u = {"id": 1, "first_name": "Оля", "username": "olya"}
bot.handle({"message": {"chat": {"id": 1}, "from": u, "text": "/start clip01"}}, st)
bot.handle({"callback_query": {"id": "q", "data": "stage:idea", "message": {"chat": {"id": 1}}}}, st)
assert bot.ADMIN_CODE
bot.handle({"message": {"chat": {"id": 99}, "from": {}, "text": "/admin " + bot.ADMIN_CODE}}, st)
bot.handle({"message": {"chat": {"id": 98}, "from": {}, "text": "/admin " + bot.ADMIN_CODE}}, st)
bot.handle({"message": {"chat": {"id": 97}, "from": {}, "text": "/admin wrongcode"}}, st)
assert st["admin_chats"] == [99, 98], st["admin_chats"]
for hours in (23, 47, 71, 119, 167, 239):
    st["users"]["1"]["t0"] = time.time() - hours * 3600
    bot.followups(st)
assert st["users"]["1"]["sent"] == ["d1", "d2", "d3", "d5", "d7", "d10"], st["users"]["1"]["sent"]
assert any(m == "send_video" for m, p in sent)
for k in bot.TEXT["objections"]:
    bot.handle({"callback_query": {"id": "o", "data": "obj:" + k, "message": {"chat": {"id": 1}}}}, st)
assert st["users"]["1"]["obj"] == list(bot.TEXT["objections"])
assert bot.parse_phone("+7 (999) 000-00-00") == "+79990000000" and bot.parse_phone("8 999 0000000") == "89990000000"
assert bot.parse_phone("привет 123") is None and bot.parse_phone("12345") is None
bot.handle({"callback_query": {"id": "q2", "data": "consult", "message": {"chat": {"id": 1}}}}, st)
bot.handle({"message": {"chat": {"id": 1}, "from": u, "text": "а сколько стоит курс?"}}, st)
assert "await_phone" not in st["users"]["1"] and "phone" not in st["users"]["1"]
bot.handle({"callback_query": {"id": "q2", "data": "consult", "message": {"chat": {"id": 1}}}}, st)
bot.handle({"message": {"chat": {"id": 1}, "from": u, "text": "+7 999 000-00-00"}}, st)
assert st["users"]["1"]["phone"] == "+79990000000"
bot.handle({"callback_query": {"id": "s", "data": "slot:evening", "message": {"chat": {"id": 1}}}}, st)
assert st["users"]["1"]["slot"] == bot.TEXT["slots"]["evening"]
# a brand that already sells gets only the consultation follow-up
bot.handle({"message": {"chat": {"id": 3}, "from": {"first_name": "Бренд"}, "text": "/start vk_1"}}, st)
bot.handle({"callback_query": {"id": "b", "data": "stage:brand", "message": {"chat": {"id": 3}}}}, st)
for hours in (23, 47, 71, 119, 167, 239):
    st["users"]["3"]["t0"] = time.time() - hours * 3600
    bot.followups(st)
got = [p.get("text", "")[:20] for m, p in sent if p.get("chat_id") == 3 and m == "sendMessage"]
assert sum(t.startswith("Хочешь разобрать") for t in got) == 1 and not any(t.startswith("Расскажу") for t in got), got
bot.handle({"message": {"chat": {"id": 99}, "from": {}, "text": "/stats"}}, st)
bot.handle({"message": {"chat": {"id": 2}, "from": {"first_name": "Бренд"}, "text": "/start"}}, st)
bot.handle({"callback_query": {"id": "q3", "data": "stage:brand", "message": {"chat": {"id": 2}}}}, st)
for m, p in sent:
    t = p.get("text", p.get("file", ""))
    print(f"{m:20} -> {p.get('chat_id')}: {str(t)[:90]!r}")
lead_to = [p["chat_id"] for m, p in sent if str(p.get("text", "")).startswith("Заявка")]
assert lead_to == [99, 98], lead_to
lead = next(p["text"] for m, p in sent if str(p.get("text", "")).startswith("Заявка"))
assert "Не знаю, где искать фабрику" in lead and "clip01" in lead, lead
assert sum("удобно Вечером" in str(p.get("text", "")) for m, p in sent) == 2
bot.handle({"message": {"chat": {"id": 98}, "from": {}, "text": "/admin_off"}}, st)
assert st["admin_chats"] == [99], st["admin_chats"]
old = {"admin_chat": 5}
assert bot.admins(old) == [5] and "admin_chat" not in old
print("admins ok: both get leads, wrong code ignored, /admin_off works, old state migrates")
# /backup: admins only, both files
n = len(sent)
bot.handle({"message": {"chat": {"id": 1}, "from": u, "text": "/backup"}}, st)
assert not any(m == "send_document" for m, p in sent[n:])
bot.handle({"message": {"chat": {"id": 99}, "from": {}, "text": "/backup"}}, st)
assert [p["file"] for m, p in sent[n:] if m == "send_document"] == ["test_state.json", "test_leads.csv"]
# first start on a new host: DATA_DIR empty, files come back by file_id from migrate.json
import json, tempfile, io
tmp = Path(tempfile.mkdtemp())
bot.DATA, bot.STATE_F, bot.LEADS_F = tmp, tmp / "state.json", tmp / "leads.csv"
mig = bot.HERE / "migrate.json"
had_mig = mig.read_bytes() if mig.exists() else None
mig.write_text(json.dumps({"state.json": "F1", "leads.csv": "F2"}), encoding="utf-8")
bot.call = lambda m, _http_timeout=70, **p: {"file_path": "documents/" + p["file_id"]}
blobs = {"F1": json.dumps({"offset": 7, "users": {"5": {"id": 5}}, "file_ids": {}, "admin_chats": [99]}).encode(),
         "F2": "время;событие\n".encode("utf-8-sig")}
bot.urllib.request.urlopen = lambda url, timeout=0: io.BytesIO(blobs[url.rsplit("/", 1)[1]])
try:
    bot.restore_from_telegram()
    assert bot.load_state()["users"]["5"] == {"id": 5} and bot.LEADS_F.exists()
    bot.restore_from_telegram()  # state exists now: nothing is overwritten
finally:
    mig.write_bytes(had_mig) if had_mig is not None else mig.unlink()
print("backup + restore ok")

# channel autopost: preview a day ahead, publish on time, holds, missed posts, failures reported once
bot.POSTS = {"channel": "@chan", "posts": [
    {"id": "p01", "at": "2026-10-26 12:00", "title": "Текст", "text": "<b>Привет</b>", "buttons": [["Смета", "https://t.me/x"]]},
    {"id": "p02", "at": "2026-10-26 19:00", "title": "Клип", "text": "клип", "video": "05-tirazh.mp4",
     "video_meta": {"duration": 44, "width": 1080, "height": 1920}, "buttons": []},
    {"id": "p03", "at": "2026-10-27 12:00", "title": "Опрос", "poll": {"question": "Этап?", "options": ["А", "Б"]}, "buttons": []},
    {"id": "p04", "at": "2026-10-27 13:00", "title": "Кейс", "text": "кейс", "hold": True, "buttons": []}]}
stc = {"users": {}, "file_ids": {}, "admin_chats": [99]}
sent.clear()
bot.call = lambda m, _http_timeout=70, **p: sent.append((m, p)) or {"message_id": 500 + len(sent)}
t01 = bot.post_time(bot.POSTS["posts"][0])
assert time.strftime("%Y-%m-%d %H:%M", time.gmtime(t01 + 3 * 3600)) == "2026-10-26 12:00"
bot.channel_posts(stc, now=t01 - 20 * 3600)                     # day before: previews only
assert not stc["posted"] and stc["previewed"] == ["p01"]          # p02 is 27 h away: no preview yet
assert any("Завтра в @chan" in p.get("text", "") for m, p in sent)
bot.channel_posts(stc, now=t01 + 60)                            # 12:01: the text goes out with HTML and a button
m, p = next((m, p) for m, p in sent if p.get("chat_id") == "@chan")
assert m == "sendMessage" and p["parse_mode"] == "HTML" and p["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/x"
assert "p01@chan" in stc["posted"] and "p02@chan" not in stc["posted"]
assert any("t.me/chan/" in p.get("text", "") for m, p in sent)  # admins get the link
bot.channel_posts(stc, now=bot.post_time(bot.POSTS["posts"][1]) + 60)
assert any(m == "send_video" and p["chat_id"] == "@chan" for m, p in sent)
t03 = bot.post_time(bot.POSTS["posts"][2])
bot.channel_posts(stc, now=t03 + 60)
poll = next(p for m, p in sent if m == "sendPoll")
assert poll["options"] == [{"text": "А"}, {"text": "Б"}]
bot.channel_posts(stc, now=t03 + 7200)                          # p04 is on hold: not published
assert "p04@chan" not in stc["posted"]
assert "⏸" in bot.plan(stc) and "p04" in bot.plan(stc)
bot.admin_post_cmd("/unhold p04", 99, stc)
bot.channel_posts(stc, now=t03 + 7200)
assert "p04@chan" in stc["posted"]
# a post that is 13 h late is reported, not published
st2 = {"users": {}, "file_ids": {}, "admin_chats": [99]}
bot.channel_posts(st2, now=t01 + 13 * 3600)
assert st2["posted"]["p01@chan"] == "missed"
# failure: reported once, retried after 10 minutes
st3 = {"users": {}, "file_ids": {}, "admin_chats": [99]}
calls = []
def failing(m, _http_timeout=70, **p):
    calls.append((m, p.get("chat_id"), p.get("text", "")[:20]))
    if p.get("chat_id") == "@chan":
        raise RuntimeError("sendMessage: 403 bot is not a member of the channel chat")
    return {"message_id": 1}
bot.call = failing
bot.channel_posts(st3, now=t01 + 60); bot.channel_posts(st3, now=t01 + 120)
tries = lambda: sum(1 for c in calls if c[1] == "@chan")
notices = lambda: sum(1 for c in calls if c[2].startswith("Не получилось"))
assert tries() == 1 and notices() == 1 and st3["post_failed"] == ["p01@chan"]   # retry waits 10 min, one notice
bot.channel_posts(st3, now=t01 + 700)
assert tries() == 2 and notices() == 1                                   # retried, no second notice
# two channels: each gets the post once, links carry the channel's own source tag
bot.call = lambda m, _http_timeout=70, **p: sent.append((m, p)) or {"message_id": 900}
bot.POSTS = {"channels": [{"chat": "@a", "tag": "tg"}, {"chat": "@b", "tag": "m4ch"}], "posts": [
    {"id": "p01", "at": "2026-10-26 12:00", "title": "Т", "text": "смета https://t.me/x?start=tg курс https://c?utm_medium=channel",
     "buttons": [["Смета", "https://t.me/x?start=tg"]]}]}
sent.clear(); stc4 = {"users": {}, "file_ids": {}, "admin_chats": [99]}
bot.channel_posts(stc4, now=t01 + 60); bot.channel_posts(stc4, now=t01 + 900)
to_b = [p for m, p in sent if p.get("chat_id") == "@b"]
assert len(to_b) == 1 and "start=m4ch" in to_b[0]["text"] and "utm_medium=m4ch" in to_b[0]["text"]
assert to_b[0]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("start=m4ch")
assert sum(1 for m, p in sent if p.get("chat_id") == "@a") == 1 and set(stc4["posted"]) == {"p01@a", "p01@b"}
print("channel autopost ok")
# the real schedule parses and every clip file exists
real = json.loads((bot.HERE / "posts.json").read_text(encoding="utf-8"))
for p in real["posts"]:
    bot.post_time(p)
    assert not p.get("video") or (bot.HERE / "video" / p["video"]).exists(), p["video"]
print("posts.json ok:", len(real["posts"]), "posts")
print("sent followups user1:", st["users"]["1"]["sent"])
print(bot.LEADS_F.read_text(encoding="utf-8-sig"))
for f in (bot.STATE_F, bot.LEADS_F):
    f.unlink(missing_ok=True)
