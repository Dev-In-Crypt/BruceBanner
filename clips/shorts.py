"""Upload to TikTok, YouTube Shorts and X through the account's AdsPower profile (docs/CLIPPING_PLAN.md).

Same browser plumbing as instagram.py: one profile at a time, a screenshot on every error.
The selectors are starting points from the web UIs (Nov 2026): verify them on a real account and fix them only here.
"""
import logging
import random
import time
from datetime import datetime, timezone

from clips import clipping, db, instagram, telegram
from clips.instagram import first, maybe

log = logging.getLogger(__name__)

TIKTOK_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload?from=webapp"
TT = {
    "file": ['input[type="file"][accept*="video"]', 'input[type="file"]'],
    "caption": ['div[contenteditable="true"].public-DraftEditor-content', 'div[contenteditable="true"]'],
    "post": ['button[data-e2e="post_video_button"]', 'button:has-text("Post")', 'button:has-text("Опубликовать")'],
    "posted": ['text=/your video (has been|is being) (uploaded|posted)/i', 'text=/manage your posts/i',
               'text=/видео опубликовано/i'],
    "video_link": 'a[href*="/video/"]',
}
YOUTUBE_UPLOAD = "https://www.youtube.com/upload"
YT = {
    "file": ['input[type="file"]'],
    "title": ['#title-textarea #textbox', 'ytcp-social-suggestions-textbox#title-textarea div[contenteditable]'],
    "description": ['#description-textarea #textbox'],
    "not_for_kids": ['tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'],
    "next": ['#next-button'],
    "public": ['tp-yt-paper-radio-button[name="PUBLIC"]'],
    "done": ['#done-button'],
    "link": 'a[href*="/shorts/"], a[href*="youtu.be/"]',
}
X_COMPOSE = "https://x.com/compose/post"
XS = {
    "file": ['input[data-testid="fileInput"]', 'input[type="file"]'],
    "text": ['div[data-testid="tweetTextarea_0"]', 'div[role="textbox"][contenteditable="true"]'],
    "post": ['button[data-testid="tweetButton"]', 'button[data-testid="tweetButtonInline"]'],
    "uploading": '[role="progressbar"]',
    "status_link": 'article a[href*="/status/"]',
}
X_MAX_CHARS = 280  # a free account's post
LOGGED_OUT = ("tiktok.com/login", "accounts.google.com", "/signin", "x.com/i/flow/login", "x.com/login")


def logged_out(page) -> bool:
    return any(s in page.url for s in LOGGED_OUT)


def wait_done(page, check, timeout_s, what):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        page.wait_for_timeout(2000)
    raise TimeoutError(f"{what}: not done in {timeout_s}s")


def upload_tiktok(page, cfg, nick, path, text) -> str:
    page.goto(TIKTOK_UPLOAD, wait_until="domcontentloaded")
    if logged_out(page):
        raise instagram.HealthProblem("logged_out")
    first(page, TT["file"], 30_000, visible=False).set_input_files(str(path))
    box = first(page, TT["caption"], 120_000)  # appears once the file is accepted
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=random.randint(20, 60))
    page.keyboard.press("Escape")  # closes the hashtag/mention suggestion list
    post = first(page, TT["post"], 30_000)
    wait_done(page, lambda: post.is_enabled() and post.get_attribute("aria-disabled") != "true",
              cfg["publish"]["upload_timeout_sec"], "tiktok upload")
    instagram.pause(cfg)
    post.click()
    first(page, TT["posted"], cfg["publish"]["upload_timeout_sec"] * 1000)
    page.goto(f"https://www.tiktok.com/@{nick}", wait_until="domcontentloaded")
    link = page.locator(TT["video_link"]).first
    link.wait_for(timeout=60_000)
    return link.get_attribute("href").split("?")[0]  # newest first; a pinned post would come before it


def upload_youtube(page, cfg, path, title, description) -> str:
    page.goto(YOUTUBE_UPLOAD, wait_until="domcontentloaded")
    if logged_out(page):
        raise instagram.HealthProblem("logged_out")
    first(page, YT["file"], 60_000, visible=False).set_input_files(str(path))
    for sel, value in ((YT["title"], title[:100]), (YT["description"], description[:4900])):
        box = first(page, sel, 120_000)
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(value, delay=random.randint(15, 40))
    first(page, YT["not_for_kids"], 30_000).click()
    for _ in range(3):  # Details -> Video elements -> Checks -> Visibility
        instagram.pause(cfg)
        first(page, YT["next"]).click()
    first(page, YT["public"], 30_000).click()
    link = page.locator(YT["link"]).first
    link.wait_for(timeout=120_000)
    url = link.get_attribute("href")
    done = first(page, YT["done"])
    wait_done(page, lambda: done.is_enabled() and done.get_attribute("aria-disabled") != "true",
              cfg["publish"]["upload_timeout_sec"], "youtube upload")
    done.click()
    page.wait_for_timeout(5000)
    return url


def upload_x(page, cfg, nick, path, text) -> str:
    page.goto(X_COMPOSE, wait_until="domcontentloaded")
    if logged_out(page):
        raise instagram.HealthProblem("logged_out")
    box = first(page, XS["text"], 30_000)
    box.click()
    page.keyboard.type(text[:X_MAX_CHARS], delay=random.randint(20, 60))
    first(page, XS["file"], 30_000, visible=False).set_input_files(str(path))
    post = first(page, XS["post"], 30_000)
    wait_done(page, lambda: post.is_enabled() and post.get_attribute("aria-disabled") != "true"
              and not page.locator(XS["uploading"]).count(), cfg["publish"]["upload_timeout_sec"], "x upload")
    instagram.pause(cfg)
    post.click()
    page.wait_for_timeout(8000)
    page.goto(f"https://x.com/{nick}", wait_until="domcontentloaded")
    links = page.locator(f'{XS["status_link"]}[href*="/{nick}/status/"]')
    links.first.wait_for(timeout=60_000)
    # a pinned post would come first: take the highest status id, which is the newest
    hrefs = [links.nth(i).get_attribute("href") for i in range(min(links.count(), 6))]
    newest = max((h.split("/status/")[1].split("/")[0] for h in hrefs), key=int)
    return f"https://x.com/{nick}/status/{newest}"


def publish_one(cfg, con, post, acc, dry=False) -> str:
    if dry:
        log.info("dry-run: would post clip post %s to %s @%s", post["id"], acc["platform"], acc["nick"])
        return "built"
    try:
        with instagram.session(cfg, acc) as page:
            if acc["platform"] == "tiktok":
                url = upload_tiktok(page, cfg, acc["nick"], post["file_path"], post["caption"])
            elif acc["platform"] == "x":
                url = upload_x(page, cfg, acc["nick"], post["file_path"], post["caption"])
            else:
                hook = con.execute("SELECT hook FROM kick_clips WHERE id = ?", (post["clip_id"],)).fetchone()[0]
                url = upload_youtube(page, cfg, post["file_path"], hook or post["caption"], post["caption"])
    except Exception as e:  # noqa: BLE001
        log.warning("clip post %s failed: %s", post["id"], e)
        with con:
            con.execute("UPDATE clip_posts SET state = 'failed', error = ? WHERE id = ?", (str(e)[:300], post["id"]))
        telegram.send(f"{acc['platform']} @{acc['nick']}: пост не вышел ({str(e)[:200]})")
        return "failed"
    with con:
        con.execute("UPDATE clip_posts SET state = 'published', post_url = ?, published_at = ? WHERE id = ?",
                    (url, db.now_iso(), post["id"]))
    log.info("clip post %s published: %s", post["id"], url)
    return "published"


def run_publish(cfg, con, dry) -> list[str]:
    now = db.iso(datetime.now(timezone.utc))
    due = con.execute("SELECT * FROM clip_posts WHERE state = 'built' AND scheduled_at <= ? ORDER BY scheduled_at",
                      (now,)).fetchall()
    out = []
    with instagram.browser_lock(cfg):
        for i, post in enumerate(due):
            if i:
                time.sleep(random.uniform(*cfg["clipping"]["between_accounts_sec"]))
            out.append(publish_one(cfg, con, post, clipping.account(cfg, post["account"]), dry))
    clipping.notify(cfg, con, dry)
    return out
