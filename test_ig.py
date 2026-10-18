"""Offline test of instagram.py: a fake Graph API, no network.

    py -3.12 -I test_ig.py
"""
import json, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import instagram as ig

calls, notes = [], []
FAKE = {"status": "IN_PROGRESS", "comments": []}


def fake_http(method, path, params=None, body=None, token=None):
    calls.append((method, path, dict(params or {}), body))
    if path == "me":
        assert token == "TOK"
        return {"user_id": 1784, "username": "anna_zheltakova"}
    if path.endswith("refresh_access_token"):
        return {"access_token": "TOK2"}
    if path == "1784/media" and method == "POST":
        return {"id": f"cont{len(calls)}"}
    if path == "1784/media" and method == "GET":
        return {"data": [{"id": "m1", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())},
                         {"id": "old", "timestamp": "2020-01-01T00:00:00+0000"}]}
    if path.startswith("cont") and method == "GET":
        return {"status_code": FAKE["status"], "status": ""}
    if path == "1784/media_publish":
        return {"id": "media9"}
    if path == "media9":
        return {"permalink": "https://www.instagram.com/reel/abc/"}
    if path == "m1/comments":
        return {"data": FAKE["comments"]}
    if path == "old/comments":
        raise AssertionError("posts older than 7 days must not be read")
    if path == "1784/messages" or path.endswith("/replies"):
        return {"ok": True}
    raise AssertionError(f"unexpected {method} {path}")


ig.http = fake_http
ig.init(lambda *a: None, lambda state, text: notes.append(text))
ig.IG = json.loads((Path(__file__).parent / "ig_posts.json").read_text(encoding="utf-8"))
first = ig.IG["posts"][0]
reel = next(p for p in ig.IG["posts"] if p["kind"] == "reel")
car = next(p for p in ig.IG["posts"] if p["kind"] == "carousel")
state = {}

# nothing happens without a token
ig.tick(state, now=ig.post_time(first) + 60)
assert not calls

assert ig.set_token(state, "TOK") == "anna_zheltakova"
assert state["ig"]["user_id"] == "1784"

# preview a day ahead
ig.tick(state, now=ig.post_time(first) - 3600)
assert any(n.startswith("Завтра в Instagram") for n in notes), notes

# carousel: one child container per slide, then the parent with all children
ig.IG["posts"] = [car]
calls.clear()
t = ig.post_time(car) + 60
ig.tick(state, now=t)
kids = [c for c in calls if c[2].get("is_carousel_item") == "true"]
parent = [c for c in calls if c[2].get("media_type") == "CAROUSEL"]
assert len(kids) == len(car["images"]) and len(parent) == 1
assert parent[0][2]["children"].count(",") == len(kids) - 1
assert kids[0][2]["image_url"].startswith("https://9008807-rgb.github.io/bpk-instagram/carousels/")
assert car["id"] in state["ig"]["pending"]

# still processing -> nothing published; finished -> published and admins get the link
ig.tick(state, now=t + 60)
assert car["id"] not in state["ig"]["posted"]
FAKE["status"] = "FINISHED"
ig.tick(state, now=t + 120)
assert state["ig"]["posted"][car["id"]] == "media9"
assert any("instagram.com/reel/abc" in n for n in notes)

# reel: REELS container with the video url and the cover offset
ig.IG["posts"] = [reel]
calls.clear()
FAKE["status"] = "IN_PROGRESS"
ig.tick(state, now=ig.post_time(reel) + 60)
rc = [c for c in calls if c[2].get("media_type") == "REELS"][0][2]
assert rc["video_url"].endswith(".mp4") and rc["thumb_offset"] == "1500" and len(rc["caption"]) <= 2200

# a container stuck in ERROR is reported once, not retried every loop
FAKE["status"] = "ERROR"
ig.tick(state, now=ig.post_time(reel) + 120)
assert reel["id"] in state["ig"]["failed"] and any("не получилось" in n for n in notes)
calls.clear()
ig.tick(state, now=ig.post_time(reel) + 180)
assert not [c for c in calls if c[2].get("media_type") == "REELS"]

# a post more than 12 h late is skipped and reported
late = dict(reel, id="ig99")
ig.IG["posts"] = [late]
ig.tick(state, now=ig.post_time(late) + 13 * 3600)
assert state["ig"]["posted"]["ig99"] == "missed"

# held post does not go out
held = dict(car, id="ig98")
ig.IG["posts"] = [held]
state["held"] = ["ig98"]
calls.clear()
ig.tick(state, now=ig.post_time(held) + 60)
assert "ig98" not in state["ig"]["pending"]

# comment keywords: any form of the word, own comments ignored, one answer per comment
assert ig.keyword("Хочу СМЕТУ!") == "смета" and ig.keyword("курс интересен") == "курс"
assert ig.keyword("Ёлки, супер") is None
FAKE["comments"] = [{"id": "c1", "text": "смету пожалуйста", "username": "masha"},
                    {"id": "c2", "text": "Смета", "username": "anna_zheltakova"},
                    {"id": "c3", "text": "красиво", "username": "olya"},
                    {"id": "c4", "text": "КУРС", "username": "lena"}]
ig.IG["posts"] = []
calls.clear()
state["ig"]["comments_at"] = 0
ig.tick(state, now=time.time())
dms = [c for c in calls if c[1] == "1784/messages"]
assert [d[3]["recipient"]["comment_id"] for d in dms] == ["c1", "c4"], dms
assert "start=ig" in dms[0][3]["message"]["text"] and "utm_source=instagram" in dms[1][3]["message"]["text"]
assert len([c for c in calls if c[1].endswith("/replies")]) == 2
calls.clear()
state["ig"]["comments_at"] = 0
ig.tick(state, now=time.time())
assert not [c for c in calls if c[1] == "1784/messages"], "answered twice"

# token refresh after 7 days
state["ig"]["token_at"] = time.time() - 8 * 86400
ig.tick(state, now=time.time())
assert state["ig"]["token"] == "TOK2"

print("plan:\n" + ig.plan(state)[:300])
print("OK")
