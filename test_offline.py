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
def fake_send(chat, path, cap, st, kind="document", markup=None, meta=None):
    assert path.exists(), path
    sent.append(("send_" + kind, {"chat_id": chat, "file": path.name, "markup": markup}))
    return {kind: {"file_id": "X"}}
bot.send_file = fake_send
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
print("sent followups user1:", st["users"]["1"]["sent"])
print(bot.LEADS_F.read_text(encoding="utf-8-sig"))
for f in (bot.STATE_F, bot.LEADS_F):
    f.unlink(missing_ok=True)
