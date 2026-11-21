import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from clips import adspower, db, instagram, telegram, video
from clips import ig_selectors as S

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


def test_no_paid_promotion_code():
    """Addendum 3: never boost or promote a reel."""
    for f in ("clips/instagram.py", "clips/ig_selectors.py", "clips/comments.py"):
        assert not re.search(r"boost|promot|продвиг|продвин", Path(f).read_text(), re.I), f


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
  <div id="s2" style="display:none"><div role="button" id="coverbtn">Select from computer</div>
    <input type="file" accept="image/*" id="cover" style="display:none">
    <div role="button" onclick="step(3)">Next</div></div>
  <div id="s3" style="display:none">
    <div contenteditable="true" role="textbox" id="cap" style="min-height:40px"></div>
    <div role="button" id="share">Share</div></div>
  <div id="done" style="display:none">Your reel has been shared.</div>
</div>
<script>
function step(n){ for (const i of [1,2,3]) document.getElementById('s'+i).style.display = i==n?'block':'none'; }
document.getElementById('create').onclick = () => document.getElementById('dlg').style.display='block';
document.getElementById('file').onchange = () => step(1);
document.getElementById('coverbtn').onclick = () => document.getElementById('cover').click();
document.getElementById('cover').onchange = (e) =>
  fetch('/__cover', {method:'POST', body: e.target.files[0].name});
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
    site = {"shared": None, "home_text": "", "bio": "old bio", "cover": None, "deleted": [], "pages": {}}
    monkeypatch.setattr(adspower, "start", lambda c, pid: chrome_ws)
    monkeypatch.setattr(adspower, "stop", lambda c, pid: None)

    def handler(route):
        req, path = route.request, route.request.url.split("instagram.com", 1)[1]
        html = None
        if path == "/__cover":
            site["cover"] = req.post_data
            return route.fulfill(status=200, body="ok")
        if path == "/__share":
            site["shared"] = req.post_data
            return route.fulfill(status=200, body="ok")
        if path == "/__delcomment":
            site["deleted"].append(req.post_data)
            return route.fulfill(status=200, body="ok")
        if path in site["pages"]:
            html = site["pages"][path]
        elif path == "/":
            html = HOME % site["home_text"]
        elif path == "/acc1/reels/":
            links = '<a href="/reel/PIN001/">p</a>' + ('<a href="/reel/NEW123/">n</a>' if site["shared"] else "")
            html = f"<html><body>{links}</body></html>"
        elif path == "/reel/PIN001/":
            html = "<html><body>old pinned reel caption</body></html>"
        elif path == "/reel/NEW123/":
            html = f"<html><body>{site['shared']}</body></html>"
        elif path == "/accounts/edit/":
            html = ('<html><body><svg aria-label="Home"></svg><textarea id="pepBio">%s</textarea>'
                    '<div role="button" onclick="fetch(\'/__bio\', {method:\'POST\', body: '
                    'document.getElementById(\'pepBio\').value})">Submit</div></body></html>') % site["bio"]
        elif path == "/__bio":
            site["bio"] = req.post_data
            return route.fulfill(status=200, body="ok")
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
    account = {**cfg["accounts"][0], "nick": "acc1", "caption_lang": "japanese", "cover": ""}
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


def test_publish_job_publishes_due_and_respects_gap(cfg, con, fake_ig):
    site, account, built = fake_ig
    cfg["accounts"] = [account]
    past = db.iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    con.execute("INSERT INTO publications (account, state, scheduled_at, file_path, caption, created_at) "
                "VALUES ('acc1', 'built', ?, ?, '朝の散歩、気持ちいい。', ?)", (past, str(built), past))
    con.execute("INSERT INTO publications (account, state, scheduled_at, file_path, caption, created_at) "
                "VALUES ('acc1', 'built', ?, ?, '二本目', ?)", (past, str(built), past))
    con.commit()
    res = instagram.run_publish(cfg, con, dry=False)
    assert res == [(1, "published")]                     # 2nd one waits: min gap between posts of one account
    assert con.execute("SELECT state FROM publications WHERE id = 2").fetchone()[0] == "built"


def test_set_bio_reserves_and_saves(cfg, con, fake_ig):
    site, account, built = fake_ig
    con.execute("INSERT INTO captions (lang, topic, text, fetched_at) VALUES ('japanese', 'anime', '海辺の朝、静かな時間。 #旅行', 'x')")
    con.commit()
    assert instagram.run_set_bio(cfg, con, account, force=False, dry=False) == "海辺の朝、静かな時間。"
    assert site["bio"] == "海辺の朝、静かな時間。"
    assert con.execute("SELECT used_for FROM captions").fetchone()[0] == "bio:acc1"
    assert con.execute("SELECT bio_set_at FROM accounts WHERE nick='acc1'").fetchone()[0]
    with pytest.raises(SystemExit, match="already set"):
        instagram.run_set_bio(cfg, con, account, force=False, dry=False)
def test_missing_file_marks_failed(cfg, con):
    account = {**cfg["accounts"][0], "nick": "acc1"}
    db.sync_accounts(con, {"accounts": [account]})
    con.execute("INSERT INTO publications (account, state, scheduled_at, file_path, created_at) "
                "VALUES ('acc1', 'built', 'x', '/nonexistent.mp4', 'x')")
    con.commit()
    pub = dict(con.execute("SELECT * FROM publications").fetchone())
    assert instagram.publish_one(cfg, con, pub, account) == "failed"
    assert con.execute("SELECT state, error FROM publications").fetchone()[1].startswith("invalid file")


def test_publish_uploads_account_cover(cfg, con, fake_ig, tmp_path):
    from PIL import Image
    site, account, built = fake_ig
    cover = tmp_path / "anime_cover.jpg"
    Image.new("RGB", (1080, 1920), (200, 50, 50)).save(cover)
    account = {**account, "cover": str(cover)}
    assert instagram.run_publish_file(cfg, con, account, built, "static", "朝の散歩。", dry=False) == "published"
    assert site["cover"] == "anime_cover.jpg"                       # our image, not a frame of the video


def test_cover_upload_failure_falls_back_unless_required(cfg, monkeypatch, tmp_path):
    class Broken:
        def set_input_files(self, *a):
            raise RuntimeError("upload failed")

        def click(self):
            raise RuntimeError("no button")

    class Page:
        def expect_file_chooser(self, timeout=0):
            from contextlib import nullcontext
            return nullcontext()

    monkeypatch.setattr(instagram, "first", lambda *a, **k: Broken())
    monkeypatch.setattr(instagram, "save_debug", lambda *a, **k: None)
    assert instagram.set_cover(Page(), cfg, "acc1", tmp_path / "c.jpg") is False   # post continues
    cfg["publish"]["require_cover"] = True
    with pytest.raises(RuntimeError):
        instagram.set_cover(Page(), cfg, "acc1", tmp_path / "c.jpg")


# ---------- addendum 3: settings check, comment moderation ----------

def settings_pages(private, search, status_text):
    sw = lambda label, on: (f'<div role="switch" aria-label="{label}" aria-checked="{str(on).lower()}"'  # noqa: E731
                            f' tabindex="0">{label}</div>')
    return {S.PRIVACY_URL: f"<html><body>{sw('Private account', private)}"
                           f"{sw('Allow public photos and videos to appear in search engine results', search)}"
                           "</body></html>",
            S.ACCOUNT_STATUS_URL: f"<html><body><svg aria-label=\"Home\"></svg>{status_text}</body></html>",
            S.AUDIENCE_URL: "<html><body>Минимальный возраст: нет. Без ограничений по странам</body></html>"}


def test_settings_check_flags_problems(cfg, con, fake_ig, monkeypatch):
    site, account, _ = fake_ig
    site["pages"] = settings_pages(True, False, "Your account can't be recommended")
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, *a, **k: sent.append(text) or True)
    assert instagram.run_health(cfg, con, account, False) == "ok"
    row = con.execute("SELECT settings_check FROM accounts WHERE nick = 'acc1'").fetchone()[0]
    assert json.loads(row) == {"private": "problem", "search_engines": "problem", "account_status": "problem",
                               "age_geo": "ok"}
    assert any("аккаунт закрыт" in t and "поисковых" in t for t in sent)
    site["pages"] = settings_pages(False, True, "Your account can be recommended")
    instagram.run_health(cfg, con, account, False)
    assert instagram.settings_problems(con, "acc1") == ([], [])


COMMENTS = """<html><body><svg aria-label="Home"></svg><ul>%s</ul>
<div role="dialog" id="menu" style="display:none">%s</div>
<script>let target = null;
function opts(n){ target = n; document.getElementById('menu').style.display = 'block'; }
function del(){ fetch('/__delcomment', {method: 'POST', body: target});
  document.getElementById('menu').style.display = 'none'; }</script></body></html>"""


def comment(n, author, text):
    return (f'<li><a href="/{author}/">{author}</a> <span dir="auto">{text}</span>'
            f'<svg aria-label="Comment options" onclick="opts(\'{n}\')" width="10" height="10"></svg></li>')


def published_pub(con, url):
    with con:
        return con.execute("INSERT INTO publications (account, state, scheduled_at, published_at, ig_url, created_at)"
                           " VALUES ('acc1', 'published', ?, ?, ?, ?)",
                           (db.now_iso(), db.now_iso(), url, db.now_iso())).lastrowid


def test_comment_moderation_deletes_only_bad_comments(cfg, con, fake_ig):
    from clips import comments
    site, account, _ = fake_ig
    rows = [comment(1, "real_fan", "ахаха лучший момент"), comment(2, "spammer", "заработок 5000 в день, в профиле"),
            comment(3, "linker", "смотри тут t.me/xxx"), comment(4, "anna_k847310", "класс")]
    site["pages"]["/reel/MOD001/"] = COMMENTS % ("".join(rows), '<button onclick="del()">Delete</button>')
    pid = published_pub(con, "https://www.instagram.com/reel/MOD001/")
    stats = comments.moderate_account(cfg, con, account, False)
    assert site["deleted"] == ["2", "3", "4"] and stats["deleted"] == 3
    rules = [r[0] for r in con.execute("SELECT rule FROM comment_actions WHERE publication_id = ? ORDER BY id", (pid,))]
    assert rules == ["spam", "link", "bot"]


def test_comment_moderation_stops_on_the_post_menu(cfg, con, fake_ig, monkeypatch):
    from clips import comments
    site, account, _ = fake_ig
    site["pages"]["/reel/MOD002/"] = COMMENTS % (comment(1, "spammer", "промокод тут"),
                                                 '<button>Archive</button><button onclick="del()">Delete</button>')
    published_pub(con, "https://www.instagram.com/reel/MOD002/")
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, *a, **k: sent.append(text) or True)
    comments.moderate_account(cfg, con, account, False)
    assert site["deleted"] == [] and any("меню поста" in t for t in sent)


def test_comment_moderation_code_never_hides_or_turns_off(cfg):
    from clips import comments
    clicks = " ".join(comments.COMMENT_OPTIONS + comments.COMMENT_DELETE + comments.LOAD_MORE).lower()
    assert not re.search(r"hide|скры|restrict|огранич|turn off|выключ|limit", clicks)
