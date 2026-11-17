import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from clips import adspower, db, instagram, video

CHROME = shutil.which("chromium") or "/opt/pw-browsers/chromium"


def test_classify():
    f = instagram.classify_health
    assert f("https://www.instagram.com/", "", True) == "ok"
    assert f("https://www.instagram.com/accounts/login/?next=/", "", False) == "logged_out"
    assert f("https://www.instagram.com/", "", False) == "logged_out"
    assert f("https://www.instagram.com/challenge/abc/", "", False) == "checkpoint"
    assert f("https://www.instagram.com/", "Введите код из письма", True) == "checkpoint"
    assert f("https://www.instagram.com/", "Try Again Later. We restrict certain activity", True) == "action_block"
    assert f("https://www.instagram.com/accounts/disabled/", "", False) == "disabled"


def test_parse_followers():
    assert instagram.parse_followers("1,234 Followers, 56 Following, 7 Posts") == 1234
    assert instagram.parse_followers("12.5K Followers, 3 Following") == 12500
    assert instagram.parse_followers("1 234 подписчиков, 56 подписок") == 1234
    assert instagram.parse_followers("nothing here") is None


def test_no_post_takedown_code():
    """H1: nothing in the browser code may edit or take down a published post."""
    for f in ("clips/instagram.py", "clips/ig_selectors.py"):
        assert not re.search(r"delete|archive|remove", Path(f).read_text(), re.I), f


# ---------- AdsPower client against a fake Local API ----------

@pytest.fixture
def fake_adspower(cfg):
    calls, state = [], {"active": False, "start_code": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            if "/active" in self.path:
                body = {"code": 0, "data": {"status": "Active" if state["active"] else "Inactive"}}
            elif "/start" in self.path:
                body = {"code": state["start_code"], "msg": "profile is open on another device",
                        "data": {"ws": {"puppeteer": "ws://127.0.0.1:1/devtools/browser/x"}}}
            else:
                body = {"code": 0, "data": {}}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg["adspower"]["base_url"] = f"http://127.0.0.1:{srv.server_port}"
    cfg["adspower"]["request_interval_sec"] = 0
    yield calls, state
    srv.shutdown()


def test_adspower_client(cfg, fake_adspower, monkeypatch):
    calls, state = fake_adspower
    monkeypatch.setenv("ADSPOWER_API_KEY", "k123")
    assert adspower.start(cfg, "p1").startswith("ws://")
    assert "headless=1" in calls[-1][0] and calls[-1][1] == "Bearer k123"
    state["active"] = True
    with pytest.raises(adspower.ProfileBusy):              # open here: never touch it
        adspower.start(cfg, "p1")
    state.update(active=False, start_code=-1)
    with pytest.raises(adspower.AdsPowerError, match="another device"):
        adspower.start(cfg, "p1")


# ---------- end-to-end publish against a fake Instagram in a real browser ----------

HOME = """<html><body><svg aria-label="Home" width=20 height=20></svg>
<div role="button" id="create"><svg aria-label="New post" width=20 height=20></svg></div>
<div style="height:3000px">feed %s</div>
<div role="dialog" id="dlg" style="display:none">
  <input type="file" id="file" style="display:none">
  <div id="s1" style="display:none"><div role="button" onclick="step(2)">Next</div></div>
  <div id="s2" style="display:none"><div role="button" onclick="step(3)">Next</div></div>
  <div id="s3" style="display:none">
    <div contenteditable="true" role="textbox" id="cap" style="min-height:40px"></div>
    <div role="button" id="share">Share</div></div>
  <div id="done" style="display:none">Your reel has been shared.</div>
</div>
<script>
function step(n){ for (const i of [1,2,3]) document.getElementById('s'+i).style.display = i==n?'block':'none'; }
document.getElementById('create').onclick = () => document.getElementById('dlg').style.display='block';
document.getElementById('file').onchange = () => step(1);
document.getElementById('share').onclick = async () => {
  await fetch('/__share', {method:'POST', body: document.getElementById('cap').innerText});
  document.getElementById('s3').style.display='none'; document.getElementById('done').style.display='block'; };
</script></body></html>"""


@pytest.fixture
def chrome_ws(tmp_path):
    if not Path(CHROME).exists():
        pytest.skip("no chromium")
    p = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--remote-debugging-port=0",
                          f"--user-data-dir={tmp_path / 'profile'}", "about:blank"],
                         stderr=subprocess.PIPE, text=True)
    for line in p.stderr:
        if m := re.search(r"(ws://\S+)", line):
            break
    yield m.group(1)
    p.kill()


@pytest.fixture
def fake_ig(cfg, con, monkeypatch, chrome_ws, media):
    """AdsPower returns our local Chromium; instagram.com is answered by a fake site."""
    site = {"shared": None, "home_text": ""}
    monkeypatch.setattr(adspower, "start", lambda c, pid: chrome_ws)
    monkeypatch.setattr(adspower, "stop", lambda c, pid: None)

    def handler(route):
        req, path = route.request, route.request.url.split("instagram.com", 1)[1]
        html = None
        if path == "/__share":
            site["shared"] = req.post_data
            return route.fulfill(status=200, body="ok")
        if path == "/":
            html = HOME % site["home_text"]
        elif path == "/acc1/reels/":
            links = '<a href="/reel/PIN001/">p</a>' + ('<a href="/reel/NEW123/">n</a>' if site["shared"] else "")
            html = f"<html><body>{links}</body></html>"
        elif path == "/reel/PIN001/":
            html = "<html><body>old pinned reel caption</body></html>"
        elif path == "/reel/NEW123/":
            html = f"<html><body>{site['shared']}</body></html>"
        elif path == "/acc1/":
            html = '<html><head><meta name="description" content="1,234 Followers, 5 Following"></head></html>'
        route.fulfill(status=200 if html else 404, content_type="text/html; charset=utf-8", body=html or "nf")

    real = instagram.session

    @contextmanager
    def routed(c, account):
        with real(c, account) as page:
            page.route("https://www.instagram.com/**", handler)
            yield page

    monkeypatch.setattr(instagram, "session", routed)
    cfg["publish"].update(pause_sec=[0, 0], warmup_sec=[0, 0.1], typing_delay_ms=[0, 1], upload_timeout_sec=20)
    account = {**cfg["accounts"][0], "nick": "acc1", "caption_lang": "japanese"}
    db.sync_accounts(con, {"accounts": [account]})
    built = cfg["data"] / "build" / "pub_1.mp4"
    video.build(media["src8"], account, cfg, built)
    return site, account, built


def test_publish_end_to_end(cfg, con, fake_ig):
    site, account, built = fake_ig
    caption = "海辺の朝、静かな時間。\n#旅行 #朝"
    assert instagram.run_publish_file(cfg, con, account, built, "static", caption, dry=False) == "published"
    assert instagram.squash(site["shared"]) == instagram.squash(caption)     # Japanese + line break typed
    row = con.execute("SELECT * FROM publications").fetchone()
    assert row["state"] == "published" and row["ig_shortcode"] == "NEW123"  # pinned PIN001 skipped
    assert row["share_clicked_at"] and row["ig_url"].endswith("/reel/NEW123/")
    assert con.execute("SELECT followers FROM accounts").fetchone()[0] == 1234
    assert not built.exists()                                                # build file cleaned up
    with pytest.raises(SystemExit, match="already published"):              # no double post
        instagram.run_publish_file(cfg, con, account, built, "static", caption, dry=False)


def test_publish_dry_run_stops_before_share(cfg, con, fake_ig):
    site, account, built = fake_ig
    ro = db.connect(cfg["data"] / "t.db", readonly=True)
    assert instagram.run_publish_file(cfg, ro, account, built, "static", "テスト投稿です", dry=True) == "built"
    assert site["shared"] is None and built.exists()
    assert any(p.name.endswith("_acc1_before_share.png") for p in (cfg["data"] / "screenshots").iterdir())


def test_action_block_pauses_account(cfg, con, fake_ig):
    site, account, built = fake_ig
    site["home_text"] = "Try Again Later. We restrict certain activity to protect our community."
    assert instagram.run_publish_file(cfg, con, account, built, "static", "テスト", dry=False) == "failed"
    acc = con.execute("SELECT status, status_reason, paused_until FROM accounts").fetchone()
    assert (acc["status"], acc["status_reason"]) == ("paused", "action_block") and acc["paused_until"]
    assert site["shared"] is None
    assert instagram.publish_one(cfg, con, {"id": 99, "file_path": str(built), "caption": "x"}, account) == "skipped"


def test_missing_file_marks_failed(cfg, con):
    account = {**cfg["accounts"][0], "nick": "acc1"}
    db.sync_accounts(con, {"accounts": [account]})
    con.execute("INSERT INTO publications (account, state, scheduled_at, file_path, created_at) "
                "VALUES ('acc1', 'built', 'x', '/nonexistent.mp4', 'x')")
    con.commit()
    pub = dict(con.execute("SELECT * FROM publications").fetchone())
    assert instagram.publish_one(cfg, con, pub, account) == "failed"
    assert con.execute("SELECT state, error FROM publications").fetchone()[1].startswith("invalid file")
