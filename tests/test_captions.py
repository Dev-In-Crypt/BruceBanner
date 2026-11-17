from clips import captions

B = ["公式", "official", "ディズニー", "Disney", "disneyjp"]
EX1 = "サンセットに染まるサントロペで過ごす、忘れられない特別な夜。優雅なシルクの輝きと夕陽の黄金色、そして沢な雰囲気が、まるで夢のようなひとときを演出しました。"
EX2 = "🦕興行収入100億円突破 ・ 時が流れても変わらないものとは...? ・ かつて子どもだったすべての人へ #トイストーリー30周年 #トイストーリー5 #ディズニー写真部"


def test_clean_caption(cfg):
    f = lambda t, lang="japanese": captions.clean_caption(t, lang, B, cfg)  # noqa: E731
    assert f(EX1) == EX1
    r = f(EX2)
    assert "#ディズニー写真部" not in r and "#トイストーリー5" in r
    assert f("【ディズニー公式】\nトイストーリー5、ついに公開！楽しみ\nhttps://disney.jp @disneyjp\n#映画 #ディズニー") \
        == "トイストーリー5、ついに公開！楽しみ\n\n#映画"
    assert f("Disney Japan official\n夏の思い出をもう一度、海辺の風景とともに") == "夏の思い出をもう一度、海辺の風景とともに"
    assert f("ディズニーの夏、始まる") is None
    assert f("Beautiful sunset at the beach tonight") is None
    assert f("戦争の映画を観た夜、静かに考える") is None
    assert f("短い") is None
    assert f("Подпишись @memes_ru! Вечер у моря, тёплый ветер и тишина https://t.me/x", "russian") \
        == "Подпишись ! Вечер у моря, тёплый ветер и тишина"
    cfg["captions"]["keep_hashtags"] = False
    assert "#" not in f(EX2)


def add(con, *texts, lang="japanese", source="d"):
    for t in texts:
        con.execute("INSERT INTO captions (lang, text, source, fetched_at) VALUES (?, ?, ?, 'x')", (lang, t, source))
    con.commit()


def test_take_caption_once(con):
    add(con, "海辺の朝。", "山の夜。")
    got = [captions.take_caption(con, "japanese", "pub:1") for _ in range(3)]
    assert got[2] is None and {g[1] for g in got[:2]} == {"海辺の朝。", "山の夜。"}
    assert captions.take_caption(con, "none", "pub:1") == (None, "")


def test_bio_reserve_release(con, cfg):
    add(con, "あ" * 200, "ディズニーの夏の思い出、また来年も", "海辺の朝、静かな時間が流れる。 #旅行")
    cid, bio = captions.reserve_bio(con, cfg, "japanese", "acc1")
    assert bio == "海辺の朝、静かな時間が流れる。"                       # short, no hashtag, no brand
    assert captions.take_caption(con, "japanese", "pub:9")[1] != "海辺の朝、静かな時間が流れる。 #旅行"
    captions.release_bio(con, cid)
    assert captions.take_caption(con, "japanese", "pub:9")[0] == cid  # free again after release


def test_harvest_from_file(cfg, con, tmp_path):
    import json
    items = [{"ownerUsername": "disneyjp", "ownerFullName": "ディズニー公式", "likesCount": 5000, "caption": EX2, "url": "u1"},
             {"ownerUsername": "x", "likesCount": 10, "caption": EX1, "url": "u2"},           # not popular
             {"ownerUsername": "y", "likesCount": -1, "caption": EX1, "url": "u3"}]            # hidden likes: kept
    f = tmp_path / "c.json"
    f.write_text(json.dumps(items, ensure_ascii=False))
    captions.harvest(cfg, con, False, f)
    captions.harvest(cfg, con, False, f)                                                   # idempotent
    assert con.execute("SELECT COUNT(*) FROM captions").fetchone()[0] == 2
