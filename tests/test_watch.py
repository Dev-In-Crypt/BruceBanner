from types import SimpleNamespace as NS

from clips import telegram, watch


def camp(i, title, platforms=("tiktok",), labels=()):
    return {"_id": i, "slug": f"s-{i}", "title": title, "costPer1kViews": 2.0, "totalBudget": 6400.0,
            "acceptedPlatforms": list(platforms), "labels": list(labels)}


def test_watch_alerts_only_new_wanted_campaigns(cfg, con, monkeypatch):
    live = [camp("a", "Audio Campaign")]
    calls = []

    def post(url, json, timeout):
        calls.append(json)
        if json["path"] == "campaigns:listDiscoveryLabels":
            value = [{"_id": "L1", "name": "Movie"}]
        else:
            assert json["args"]["status"] == "active" and json["args"]["paginationOpts"]["numItems"] == 50
            value = {"page": live, "isDone": True, "continueCursor": "[]"}
        return NS(raise_for_status=lambda: None, json=lambda: {"status": "success", "value": value})

    monkeypatch.setattr(watch.requests, "post", post)
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, **kw: sent.append(text) or True)
    assert len(watch.run(cfg, con, False)) == 1 and sent == []     # first run: remembered, no flood
    live += [camp("b", "Movie Trailer", ("tiktok", "youtube"), [{"_id": "L1", "name": "Movie"}]),
             camp("c", "IG only", ("instagram",))]
    del live[2]["slug"]                                               # some campaigns have no slug: the id is the URL
    new = watch.run(cfg, con, False)
    assert {c["id"] for c in new} == {"b", "c"}
    assert len(sent) == 1                                            # instagram-only campaign: no alert
    assert sent[0].startswith("🔥 promote.fun: новая кампания «Movie Trailer» [Movie]")
    assert "$2.0 за 1 000 просмотров, бюджет $6,400, TikTok, YouTube" in sent[0] and "/campaigns/s-b" in sent[0]
    assert watch.run(cfg, con, False) == [] and len(sent) == 1       # nothing new, nothing sent
