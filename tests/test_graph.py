from types import SimpleNamespace as NS

from clips import graph, trends


def test_graph_reels(cfg, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_GRAPH_TOKEN", "t")
    cfg["graph"] = {"base_url": "https://g"}
    cfg["trends"]["source"] = "graph"
    reel = lambda code, ts, **kw: {"permalink": f"https://www.instagram.com/reel/{code}/", "timestamp": ts,  # noqa: E731
                                   "media_product_type": "REELS", "view_count": 5000, "like_count": 70, **kw}
    pages = {None: {"data": [reel("A", "2099-01-02T00:00:00+0000", media_url="https://cdn/a.mp4", caption="hi"),
                             {"permalink": "https://www.instagram.com/p/PHOTO/", "media_product_type": "FEED",
                              "timestamp": "2099-01-02T00:00:00+0000"}],
                    "paging": {"cursors": {"after": "C1"}}},
             "C1": {"data": [reel("B", "2099-01-01T00:00:00+0000"),          # music rights: no media_url
                             reel("OLD", "2001-01-01T00:00:00+0000")]}}
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params["fields"]))
        if url.endswith("/me/accounts"):
            return NS(json=lambda: {"data": [{"id": "page"}, {"instagram_business_account": {"id": "IG1"}}]})
        if "username(notpro)" in params["fields"]:
            return NS(json=lambda: {"error": {"message": "not a business account"}})
        after = "C1" if ".after(C1)" in params["fields"] else None
        return NS(json=lambda: {"business_discovery": {"media": pages[after]}})

    monkeypatch.setattr(graph.requests, "get", fake_get)
    items = trends.reels(cfg, ["Donor", "notpro"], limit=10, days=30)
    assert [i["shortCode"] for i in items] == ["A", "B"]          # photo dropped, old reel dropped, bad donor skipped
    a, b = items
    assert (a["ownerUsername"], a["videoUrl"], a["videoPlayCount"], a["likesCount"], a["caption"]) == \
        ("donor", "https://cdn/a.mp4", 5000, 70, "hi")
    assert b["videoUrl"] is None and b["videoDuration"] is None   # downloaded by yt-dlp, duration checked after
    assert calls[1][0] == "https://g/IG1" and "business_discovery.username(Donor)" in calls[1][1]
