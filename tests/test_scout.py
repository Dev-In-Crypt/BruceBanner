import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest

from clips import instagram, scout, trends

NOW = datetime.now(timezone.utc)


def media(code, hours_ago, **kw):
    return {"code": code, "media_type": 2, "taken_at": int((NOW - timedelta(hours=hours_ago)).timestamp()),
            "play_count": 5000, "like_count": 70, "caption": {"text": f"cap {code}"},
            "video_versions": [{"url": f"https://cdn/{code}.mp4"}], "video_duration": 21.5, **kw}


def test_collect_reads_rest_and_graphql_shapes():
    rest = {"items": [{"media": media("A", 5)}, {"media": media("PHOTO", 5, media_type=1)}]}
    gql = {"data": {"xdt_api__v1__clips__user__connection_v2": {"edges": [
        {"node": {"media": media("B", 6, play_count=None, ig_play_count=0, caption=None, video_versions=[])}}]}}}
    got = scout.collect([json.dumps(rest), "not json", json.dumps(gql)], "donor")
    assert set(got) == {"A", "B"}                                   # photo dropped, bad body ignored
    a, b = got["A"], got["B"]
    assert (a["videoUrl"], a["caption"], a["videoPlayCount"], a["videoDuration"]) == ("https://cdn/A.mp4", "cap A", 5000, 21.5)
    assert b["videoPlayCount"] == 0 and b["videoUrl"] is None and b["caption"] is None   # 0 views kept, yt-dlp later
    assert datetime.fromisoformat(a["timestamp"]) < NOW and a["ownerUsername"] == "donor"


class FakePage:
    """Each donor page answers with one JSON script; a scroll adds an older batch."""

    def __init__(self, pages, health="ok"):
        self.pages, self.health, self.url, self.scrolls = pages, health, "", 0

    def on(self, *a):
        pass

    def goto(self, url, **kw):
        self.url, self.scrolls = url, 0

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        name = self.url.rstrip("/").split("/")[-2].lower()
        batches = self.pages.get(name, [])[: self.scrolls + 1]
        return type("L", (), {"all_inner_texts": lambda _: [json.dumps({"items": b}) for b in batches]})()

    @property
    def mouse(self):
        page = self

        class M:
            def wheel(self, *a):
                page.scrolls += 1
        return M()


@pytest.fixture
def scout_cfg(cfg, monkeypatch):
    cfg["trends"]["source"] = "browser"
    cfg["scout"].update(adspower_id="s1", pause_sec=[0, 0], between_donors_sec=[0, 0])
    monkeypatch.setattr(instagram, "browser_lock", lambda c: nullcontext())
    return cfg


def test_scout_reels_limit_age_and_bad_donor(scout_cfg, monkeypatch):
    page = FakePage({"donor": [[media("N1", 2), media("N2", 30)], [media("OLD", 24 * 10)]], "empty": []})
    monkeypatch.setattr(instagram, "session", lambda c, acc: nullcontext(page))
    monkeypatch.setattr(instagram, "check_health", lambda p: p.health)
    items = trends.reels(scout_cfg, ["empty", "Donor"], limit=10, days=3)
    assert [i["shortCode"] for i in items] == ["N1", "N2"]          # older than 3 days dropped, empty donor skipped
    assert page.scrolls == 1                                          # stopped scrolling once it reached old reels


def test_scout_stops_when_profile_is_blocked(scout_cfg, monkeypatch):
    page = FakePage({}, health="checkpoint")
    alerts = []
    monkeypatch.setattr(instagram, "session", lambda c, acc: nullcontext(page))
    monkeypatch.setattr(instagram, "check_health", lambda p: p.health)
    monkeypatch.setattr(instagram, "alert", lambda text, **kw: alerts.append(text))
    with pytest.raises(scout.ScoutDown):
        trends.reels(scout_cfg, ["donor"], limit=10)
    assert alerts and "checkpoint" in alerts[0]


def test_scout_needs_a_profile(cfg):
    cfg["trends"]["source"] = "browser"
    with pytest.raises(RuntimeError, match="scout.adspower_id"):
        trends.reels(cfg, ["donor"], limit=10)
