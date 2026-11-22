"""Browser work through AdsPower: health check (spec module 11) and publishing (module 10).

H1: there is no code here that edits or takes down a published post. Keep it that way.
"""
import fcntl
import json
import logging
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from clips import adspower, db, telegram, video
from clips import ig_selectors as S

log = logging.getLogger(__name__)
IG = "https://www.instagram.com"


class HealthProblem(Exception):
    """Instagram shows a problem; args[0] is the health string from classify_health."""


# ---------- browser plumbing ----------

@contextmanager
def browser_lock(cfg):
    """H7: at most one AdsPower profile open at a time, across all commands."""
    f = open(cfg["data"] / "locks" / "browser.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        log.info("another browser job is running, nothing to do")
        raise SystemExit(0)
    try:
        yield
    finally:
        f.close()
# ponytail: one profile at a time, add a counting semaphore if throughput ever needs 2-3 profiles


def save_debug(cfg, page, nick, step) -> Path | None:
    """H9: screenshot + HTML. Never raises: a failing screenshot must not hide the real error."""
    base = cfg["data"] / "screenshots" / f"{datetime.now():%Y%m%d-%H%M%S}_{nick}_{step}"
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
        log.info("debug saved: %s.png", base)
        return Path(f"{base}.png")
    except Exception as e:  # noqa: BLE001
        log.warning("could not save debug for %s/%s: %s", nick, step, e)
        return None


@contextmanager
def session(cfg, account):
    """Open the account's AdsPower profile and yield a Playwright page; always close the profile."""
    ws = adspower.start(cfg, account["adspower_id"])
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.set_default_timeout(30_000)
            try:
                yield page
            except Exception:
                save_debug(cfg, page, account["nick"], "error")
                raise
            finally:
                page.close()
                browser.close()  # disconnects CDP only; AdsPower closes the browser below
    finally:
        adspower.stop(cfg, account["adspower_id"])


def pause(cfg):
    time.sleep(random.uniform(*cfg["publish"]["pause_sec"]))


def first(page, candidates, timeout_ms=15_000, visible=True):
    """First matching locator among alternative selectors; LookupError if none appears in time."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for sel in candidates:
            loc = page.locator(sel)
            for i in range(loc.count()):  # hidden matches (old dialog steps) may come first
                if not visible or loc.nth(i).is_visible():
                    return loc.nth(i)
        if time.monotonic() >= deadline:
            raise LookupError(f"none visible: {candidates}")
        page.wait_for_timeout(500)


def maybe(page, candidates, timeout_ms=5_000):
    """Like first(), but None instead of an error (for optional dialogs)."""
    try:
        return first(page, candidates, timeout_ms)
    except LookupError:
        return None


def squash(s: str) -> str:
    return " ".join((s or "").split())


# ---------- health (module 11) ----------

def classify_health(url: str, text: str, logged_in: bool) -> str:
    """'ok' | 'logged_out' | 'checkpoint' | 'action_block' | 'disabled'"""
    u, t = url.lower(), text.lower()
    if any(s in u for s in S.URL_DISABLED) or any(s in t for s in S.TEXT_DISABLED):
        return "disabled"
    if any(s in u for s in S.URL_CHECKPOINT) or any(s in t for s in S.TEXT_CHECKPOINT):
        return "checkpoint"
    if any(s in u for s in S.URL_LOGIN) or not logged_in:
        return "logged_out"
    if any(s in t for s in S.TEXT_ACTION_BLOCK):
        return "action_block"
    return "ok"


def page_text(page) -> str:
    try:
        return page.inner_text("body")[:20000]
    except Exception:  # noqa: BLE001
        return ""


def check_health(page) -> str:
    return classify_health(page.url, page_text(page), maybe(page, S.LOGGED_IN, 10_000) is not None)


HEALTH_STATUS = {  # health -> (DB status, Russian reason for the alert)
    "action_block": ("paused", "ограничение действий"),
    "checkpoint": ("check", "проверка (код или селфи)"),
    "logged_out": ("check", "вышел из аккаунта"),
    "disabled": ("banned", "аккаунт отключён"),
}


def alert(text, photo=None, dry=False):
    telegram.send(text, photo=photo, dry=dry)


def apply_health(con, cfg, nick, health, screenshot=None, dry=False):
    if health == "ok":
        return
    status, reason_ru = HEALTH_STATUS[health]
    until = None
    if status == "paused":
        until = db.iso(datetime.now(timezone.utc) + timedelta(hours=cfg["publish"]["action_block_pause_hours"]))
    if not dry:
        db.set_account_status(con, nick, status, health, until)
    text, buttons = telegram.msg_account(nick, status, reason_ru)
    telegram.send(text, buttons, screenshot, dry)
    from clips import submit  # account in danger: its reels with 50k+ should be submitted now
    submit.run_submit_check(cfg, con, dry, nick=nick)


SETTINGS_RU = {"private": "аккаунт закрыт", "search_engines": "запрещено появление в поисковых системах",
               "account_status": "«Статус аккаунта» не весь зелёный", "age_geo": "ограничения по возрасту или гео"}


def switch_state(page, name_re) -> bool | None:
    for role in ("switch", "checkbox"):
        loc = page.get_by_role(role, name=re.compile(name_re, re.I))
        if loc.count():
            return loc.first.is_checked()
    return None


def text_state(page, bad, ok) -> str:
    t = page_text(page).lower()
    return "problem" if any(s in t for s in bad) else "ok" if any(s in t for s in ok) else "unknown"


def check_settings(page, cfg) -> dict:
    """Addendum 3: Playerok accepts only public, searchable accounts with a green status and no age/geo limits."""
    r = {}
    page.goto(IG + S.PRIVACY_URL, wait_until="domcontentloaded")
    pause(cfg)
    private, search = switch_state(page, S.PRIVATE_SWITCH), switch_state(page, S.SEARCH_ENGINES_SWITCH)
    r["private"] = "unknown" if private is None else "problem" if private else "ok"
    r["search_engines"] = "unknown" if search is None else "ok" if search else "problem"
    page.goto(IG + S.ACCOUNT_STATUS_URL, wait_until="domcontentloaded")
    pause(cfg)
    r["account_status"] = text_state(page, S.TEXT_STATUS_BAD, S.TEXT_STATUS_OK)
    page.goto(IG + S.AUDIENCE_URL, wait_until="domcontentloaded")
    pause(cfg)
    r["age_geo"] = text_state(page, S.TEXT_AGE_GEO_BAD, S.TEXT_AGE_GEO_OK)
    return r


def settings_problems(con, nick) -> tuple[list[str], list[str]]:
    """(problems, not checked) in Russian, from the last settings check."""
    row = con.execute("SELECT settings_check FROM accounts WHERE nick = ?", (nick,)).fetchone()
    r = json.loads(row[0]) if row and row[0] else {}
    return ([SETTINGS_RU[k] for k, v in r.items() if v == "problem"],
            [SETTINGS_RU[k] for k in SETTINGS_RU if r.get(k, "unknown") == "unknown"])


def db_status(con, nick) -> str:
    row = con.execute("SELECT status FROM accounts WHERE nick = ?", (nick,)).fetchone()
    return row["status"] if row else "active"


# ---------- publishing (module 10) ----------

def set_cover(page, cfg, nick, cover) -> bool:
    """Upload the account's own cover image instead of a video frame (addendum 10b). Returns success."""
    try:
        try:
            first(page, S.COVER_FILE_INPUT, 3_000, visible=False).set_input_files(str(cover))
        except Exception:  # noqa: BLE001  (no input, or the upload through it failed): try the button
            with page.expect_file_chooser(timeout=10_000) as chooser:
                first(page, S.COVER_FROM_COMPUTER, 10_000).click()
            chooser.value.set_files(str(cover))
    except Exception as e:  # noqa: BLE001
        shot = save_debug(cfg, page, nick, "cover_failed")
        if cfg["publish"].get("require_cover"):
            raise
        log.warning("cover not set for @%s (%s), posting with Instagram's frame; screenshot %s", nick, e, shot)
        return False
    page.wait_for_timeout(2000)
    return True


def post_reel(page, cfg, nick, file_path, caption, dry, on_share=None, cover=None) -> None:
    p = cfg["publish"]

    def step(name):
        if dry:
            save_debug(cfg, page, nick, name)
        pause(cfg)

    page.goto(IG + "/", wait_until="domcontentloaded")
    health = check_health(page)
    if health != "ok":
        raise HealthProblem(health)
    end = time.monotonic() + random.uniform(*p["warmup_sec"])  # look like a person browsing the feed
    while time.monotonic() < end:
        page.mouse.wheel(0, random.randint(300, 900))
        page.wait_for_timeout(random.randint(1000, 3000))
    step("feed")
    first(page, S.CREATE).click()
    if item := maybe(page, S.CREATE_POST_ITEM):
        item.click()
    step("create")
    first(page, S.FILE_INPUT, visible=False).set_input_files(str(file_path))
    if ok := maybe(page, S.REELS_NOTICE_OK):
        ok.click()
    step("uploaded")
    if crop := maybe(page, S.CROP_BUTTON):
        crop.click()
        first(page, S.CROP_ORIGINAL).click()
    first(page, S.NEXT).click()
    pause(cfg)
    if cover:
        set_cover(page, cfg, nick, cover)
        step("cover")
    first(page, S.NEXT).click()
    step("edit")
    if caption:
        first(page, S.CAPTION).click()
        page.keyboard.type(caption, delay=random.randint(*p["typing_delay_ms"]))
    if dry:
        save_debug(cfg, page, nick, "before_share")
        page.keyboard.press("Escape")
        if cancel := maybe(page, S.CANCEL_POST, 3_000):
            cancel.click()
        log.info("dry-run: stopped before Share for @%s", nick)
        return
    if on_share:
        on_share()  # record share_clicked_at BEFORE the click: a crash after it may leave a live post
    first(page, S.SHARE).click()
    deadline = time.monotonic() + p["upload_timeout_sec"]
    while time.monotonic() < deadline:
        if maybe(page, S.SHARED_OK, 10_000):
            return
        if classify_health(page.url, page_text(page), True) == "action_block":
            raise HealthProblem("action_block")
    raise TimeoutError(f"no 'shared' confirmation within {p['upload_timeout_sec']} s")


def find_reel(page, nick, caption, known: set[str], tries=3, wait_s=20) -> str | None:
    """Shortcode of the reel we just posted. Pinned reels are why the caption is checked."""
    want = squash(caption)[:30]
    for attempt in range(tries):
        page.goto(f"{IG}/{nick}/reels/", wait_until="domcontentloaded")
        try:
            page.wait_for_selector(S.REEL_LINK, timeout=15_000)
            hrefs = [page.locator(S.REEL_LINK).nth(i).get_attribute("href")
                     for i in range(min(6, page.locator(S.REEL_LINK).count()))]
        except Exception:  # noqa: BLE001
            hrefs = []
        codes = [m.group(1) for h in hrefs if h and (m := re.search(r"/reel/([\w-]+)", h))]
        fresh = [c for c in dict.fromkeys(codes) if c not in known]
        log.debug("find_reel @%s try %s: hrefs=%s fresh=%s", nick, attempt, hrefs, fresh)
        if fresh and not want:
            # ponytail: no caption to match, a manually pinned unknown reel would be picked
            return fresh[0]
        for code in fresh:
            page.goto(f"{IG}/reel/{code}/", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            log.debug("find_reel %s text=%r want=%r", code, squash(page_text(page))[:80], want)
            if want in squash(page_text(page)):
                return code
        if attempt < tries - 1:
            time.sleep(wait_s)
    return None


MULT = {"k": 1e3, "к": 1e3, "тыс": 1e3, "m": 1e6, "м": 1e6, "млн": 1e6}


def parse_followers(text: str) -> int | None:
    m = re.search(r"(\d[\d\s.,]*?)\s*(k|m|к|м|тыс\.?|млн)?\s*(followers|подписчик)", text, re.I)
    if not m:
        return None
    num = re.sub(r"\s", "", m.group(1))
    suffix = (m.group(2) or "").lower().rstrip(".")
    if suffix:
        return int(float(num.replace(",", ".")) * MULT[suffix])
    return int(re.sub(r"[.,]", "", num))


def read_followers(page, nick) -> int | None:
    try:
        page.goto(f"{IG}/{nick}/", wait_until="domcontentloaded")
        return parse_followers(page.locator('meta[name="description"]').get_attribute("content") or "")
    except Exception as e:  # noqa: BLE001
        log.info("followers of @%s not read: %s", nick, e)
        return None


def publish_one(cfg, con, pub: dict, account: dict, dry=False) -> str:
    """Publish one built publication. Returns the new state (or 'skipped')."""
    nick, pid = account["nick"], pub["id"]
    if db_status(con, nick) != "active":
        log.info("pub=%s account=%s not active, skipped", pid, nick)
        return "skipped"
    try:  # H4: never publish an invalid file; a missing or corrupt file is a failure, not a retry
        problems = video.validate(pub["file_path"], cfg, video.probe(pub["file_path"])["duration"])
    except Exception as e:  # noqa: BLE001
        problems = [f"{type(e).__name__}: {str(e)[:200]}"]
    if problems:
        return _set(con, pid, dry, state="failed", error="invalid file: " + "; ".join(problems))
    clicked = {"at": None}

    def on_share():
        clicked["at"] = db.now_iso()
        _set(con, pid, dry, share_clicked_at=clicked["at"])

    known = {r[0] for r in con.execute("SELECT ig_shortcode FROM publications WHERE ig_shortcode IS NOT NULL")}
    try:
        with session(cfg, account) as page:
            try:
                auto = Path(pub["file_path"]).with_suffix(".jpg")  # the background, written by video.build
                post_reel(page, cfg, nick, pub["file_path"], pub["caption"] or "", dry, on_share,
                          account.get("cover") or (str(auto) if auto.is_file() else None))
            except HealthProblem as e:
                shot = save_debug(cfg, page, nick, f"health_{e.args[0]}")
                apply_health(con, cfg, nick, e.args[0], shot, dry)
                return _set(con, pid, dry, state="unconfirmed" if clicked["at"] else "failed",
                            error=f"health:{e.args[0]}")
            if dry:
                return "built"
            code = find_reel(page, nick, pub["caption"] or "", known)
            followers = read_followers(page, nick)
    except (adspower.ProfileBusy, adspower.AdsPowerError) as e:
        log.warning("pub=%s account=%s profile unavailable, retry later: %r", pid, nick, e)
        return "built"
    except Exception as e:  # noqa: BLE001  (screenshot already saved by session)
        state = "unconfirmed" if clicked["at"] else "failed"
        alert(f"⚠️ Ошибка публикации pub#{pid} @{nick}: {type(e).__name__}: {str(e)[:200]}", dry=dry)
        log.exception("pub=%s account=%s publish error", pid, nick)
        return _set(con, pid, dry, state=state, error=f"{type(e).__name__}: {str(e)[:300]}")
    if followers is not None and not dry:
        with con:
            con.execute("UPDATE accounts SET followers = ?, updated_at = ? WHERE nick = ?",
                        (followers, db.now_iso(), nick))
    if not code:
        alert(f"Рилс опубликован, но ссылку найти не удалось: @{nick} pub#{pid}. "
              "Проверю завтра по статистике.", dry=dry)
        return _set(con, pid, dry, state="unconfirmed")
    state = _set(con, pid, dry, state="published", published_at=db.now_iso(), ig_shortcode=code,
                 ig_url=f"{IG}/reel/{code}/")
    built = Path(pub["file_path"]).resolve()
    if built.is_relative_to((cfg["data"] / "build").resolve()):  # only our own build files, never user files
        built.unlink(missing_ok=True)
    log.info("pub=%s account=%s published %s/reel/%s/", pid, nick, IG, code)
    if pub.get("is_bind"):  # addendum 3: the operator confirms the binding in the Playerok bot
        alert(f"🔗 Код привязки опубликован на @{nick}: {IG}/reel/{code}/\n"
              f"Подтвердите привязку в боте Playerok, затем отправьте здесь /bound {nick}", dry=dry)
    return state


def _set(con, pid, dry, **fields) -> str | None:
    if not dry:
        with con:
            con.execute(f"UPDATE publications SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), pid))
    return fields.get("state")


# ---------- CLI commands ----------

def run_health(cfg, con, account, dry) -> str:
    nick = account["nick"]
    status = db_status(con, nick)
    if status in ("check", "banned"):  # H5: the operator resolves these first
        print(f"@{nick} has status {status}: not opening the profile until the operator resolves it")
        return status
    with browser_lock(cfg), session(cfg, account) as page:
        page.goto(IG + "/", wait_until="domcontentloaded")
        pause(cfg)
        health = check_health(page)
        shot = save_debug(cfg, page, nick, "health")
        settings = check_settings(page, cfg) if health == "ok" else None
        settings_shot = save_debug(cfg, page, nick, "settings") if settings else None
    print(health, settings or "")
    apply_health(con, cfg, nick, health, shot, dry)
    if settings is not None:
        if not dry:
            with con:
                con.execute("UPDATE accounts SET settings_check = ?, settings_checked_at = ? WHERE nick = ?",
                            (json.dumps(settings), db.now_iso(), nick))
        bad = [SETTINGS_RU[k] for k, v in settings.items() if v == "problem"]
        if bad:
            alert(f"⚠️ @{nick}: настройки мешают подаче в Playerok: {', '.join(bad)}", settings_shot, dry)
    return health


def run_publish_file(cfg, con, account, file_path, banner_type, caption, dry) -> str:
    file_path = str(Path(file_path).resolve())
    dup = con.execute("SELECT id FROM publications WHERE file_path = ? AND state IN ('published','unconfirmed')",
                      (file_path,)).fetchone()
    if dup:
        raise SystemExit(f"already published as pub#{dup['id']}: refusing to post the same file twice")
    if caption is None:
        if account["caption_lang"] != "none":
            raise SystemExit("--caption is required until the caption pool exists (phase 3)")
        caption = ""
    pub = {"id": None, "file_path": file_path, "caption": caption}
    if not dry:
        with con:
            pub["id"] = con.execute(
                "INSERT INTO publications (account, state, scheduled_at, file_path, background, banner_type, "
                "caption, caption_lang, created_at) VALUES (?, 'built', ?, ?, ?, ?, ?, ?, ?)",
                (account["nick"], db.now_iso(), file_path, Path(account["background"]).name, banner_type,
                 caption, account["caption_lang"], db.now_iso())).lastrowid
    with browser_lock(cfg):
        state = publish_one(cfg, con, pub, account, dry)
    print(f"pub#{pub['id']}: {state}")
    return state


def run_publish(cfg, con, dry) -> list:
    """Publish due built publications, one profile at a time, with pauses between accounts."""
    by_nick = {a["nick"]: a for a in cfg["accounts"]}
    results = []
    with browser_lock(cfg):
        if not dry:
            db.sync_accounts(con, cfg)
            for nick in db.resume_paused_accounts(con):
                alert(f"Аккаунт @{nick} снова активен")
        now = datetime.now(timezone.utc)
        due = con.execute(
            "SELECT p.* FROM publications p JOIN accounts a ON a.nick = p.account WHERE p.state = 'built' "
            "AND p.scheduled_at <= ? AND a.status = 'active' ORDER BY p.scheduled_at LIMIT ?",
            (db.iso(now), cfg["publish"]["max_per_run"])).fetchall()
        gap_since = db.iso(now - timedelta(minutes=cfg["planner"]["min_gap_minutes"]))
        for i, pub in enumerate(due):
            account = by_nick.get(pub["account"])
            recent = con.execute("SELECT 1 FROM publications WHERE account = ? AND published_at >= ?",
                                 (pub["account"], gap_since)).fetchone()
            if not account or recent:  # catch-up burst after downtime: keep the gap between posts
                continue
            if i:
                time.sleep(random.uniform(*cfg["publish"]["between_accounts_sec"]))
            results.append((pub["id"], publish_one(cfg, con, dict(pub), account, dry)))
    for pid, state in results:
        print(f"pub#{pid}: {state}")
    return results


def set_bio(page, cfg, nick, bio, dry) -> None:
    page.goto(f"{IG}/accounts/edit/", wait_until="domcontentloaded")
    health = check_health(page)
    if health != "ok":
        raise HealthProblem(health)
    box = first(page, S.BIO_TEXTAREA)
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.type(bio, delay=random.randint(*cfg["publish"]["typing_delay_ms"]))
    if dry:
        save_debug(cfg, page, nick, "before_bio_submit")
        return
    pause(cfg)
    first(page, S.BIO_SUBMIT).click()
    page.wait_for_timeout(3000)
    page.reload(wait_until="domcontentloaded")
    got = first(page, S.BIO_TEXTAREA).input_value()
    if squash(got) != squash(bio):
        raise RuntimeError(f"bio not saved: page shows {got[:60]!r}")


def run_set_bio(cfg, con, account, force, dry) -> str:
    from clips import captions
    nick, lang = account["nick"], account["caption_lang"]
    row = con.execute("SELECT status, bio_set_at FROM accounts WHERE nick = ?", (nick,)).fetchone()
    if lang == "none":
        raise SystemExit(f"@{nick} has caption_lang none: no bio")
    if row and row["status"] in ("check", "banned"):
        raise SystemExit(f"@{nick} has status {row['status']}: resolve it first")
    if row and row["bio_set_at"] and not force:
        raise SystemExit(f"@{nick} bio already set at {row['bio_set_at']} (use --force)")
    if dry:
        pick = next(captions.bio_candidates(con, cfg, lang, account["theme"]), None)
    else:
        pick = captions.reserve_bio(con, cfg, lang, nick, account["theme"])
    if not pick:
        raise SystemExit(f"no free {lang} caption usable as a bio: run harvest-captions")
    cid, bio = pick
    try:
        with browser_lock(cfg), session(cfg, account) as page:
            set_bio(page, cfg, nick, bio, dry)
    except BaseException:
        if not dry:
            captions.release_bio(con, cid)
        raise
    if not dry:
        with con:
            con.execute("UPDATE accounts SET bio_set_at = ?, bio_caption_id = ?, updated_at = ? WHERE nick = ?",
                        (db.now_iso(), cid, db.now_iso(), nick))
    print(f"@{nick} bio{' (dry-run)' if dry else ''}: {bio}")
    return bio
