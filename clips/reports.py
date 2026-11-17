"""Daily summary (Task 4.5) and weekly report (Task 5.2) for the operator."""
import shutil
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from clips import captions, db, telegram


def summary_text(con, cfg, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(cfg["timezone"])
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    y0, y1 = db.iso(today - timedelta(days=1)), db.iso(today)
    month0 = db.iso(today.replace(day=1))
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    per_acc = q("SELECT account, COUNT(*) FROM publications WHERE published_at >= ? AND published_at < ? "
                "GROUP BY account", y0, y1)
    failed = q("SELECT COUNT(*) FROM publications WHERE state = 'failed' AND scheduled_at >= ? AND scheduled_at < ?",
               y0, y1)[0][0]
    by_status = {}
    for r in q("SELECT nick, status FROM accounts"):
        by_status.setdefault(r["status"], []).append(r["nick"])
    submit_now = q("SELECT id, account, last_views, published_at FROM publications WHERE submit_status = 'submit_now' "
                   "ORDER BY published_at")
    cnt = lambda s: q("SELECT COUNT(*) FROM publications WHERE submit_status = ?", s)[0][0]  # noqa: E731
    approved = q("SELECT COALESCE(SUM(amount_rub), 0) FROM publications WHERE submit_status = 'approved' "
                 "AND decided_at >= ?", month0)[0][0]
    accs = lambda s: f"{len(by_status.get(s, []))}" + (  # noqa: E731
        f" ({', '.join('@' + n for n in by_status[s])})" if by_status.get(s) and s != "active" else "")
    lines = [
        f"📊 Сводка за {today:%d.%m.%Y}",
        f"Опубликовано вчера: {sum(r[1] for r in per_acc)}"
        + (f" ({', '.join(f'@{r[0]}: {r[1]}' for r in per_acc)})" if per_acc else ""),
        f"Ошибки вчера: {failed}",
        f"Аккаунты: активны {accs('active')}, пауза {accs('paused')}, проверка {accs('check')}, бан {accs('banned')}",
        f"Пора подавать: {len(submit_now)}",
    ]
    for r in submit_now:
        by = db.parse_iso(r["published_at"]).astimezone(tz) + timedelta(days=cfg["playerok"]["submit_by_day"])
        lines.append(f"  pub#{r['id']} @{r['account']} {telegram.num(r['last_views'] or 0)} просм., подать до {by:%d.%m}")
    lines.append(f"Кандидаты: {cnt('candidate')} · Поданы, ждут решения: {cnt('submitted')}")
    lines.append(f"Одобрено в этом месяце: {telegram.num(approved)} ₽")
    langs = captions.languages_in_use(cfg)
    if langs:
        parts = []
        for lang in langs:
            n = captions.free_count(con, lang)
            parts.append(("⚠️ " if n < cfg["captions"]["low_pool_alert"] else "") + f"{lang} {n}")
        lines.append("Свободных подписей: " + ", ".join(parts))
    lines.append(f"Свободно на диске: {shutil.disk_usage(cfg['data']).free / 2**30:.0f} ГБ")
    return "\n".join(lines)


def run_summary(cfg, con, dry):
    telegram.send(summary_text(con, cfg), dry=dry)


# ---------- weekly report (Task 5.2) ----------

GROUPS = (("account", "Аккаунты"), ("donor", "Доноры"), ("background", "Фоны"), ("banner_type", "Баннер"),
          ("caption_lang", "Язык подписи"))


def weekly(con, cfg, now=None) -> str:
    import html
    import statistics
    now = now or datetime.now(timezone.utc)
    since, min_paid = db.iso(now - timedelta(days=7)), cfg["playerok"]["min_paid_views"]
    rows = con.execute("SELECT p.*, s.donor FROM publications p LEFT JOIN sources s ON s.id = p.source_id "
                       "WHERE p.published_at >= ?", (since,)).fetchall()
    num = telegram.num
    out = [f"📈 Неделя до {now.astimezone(ZoneInfo(cfg['timezone'])):%d.%m.%Y}"]
    views = [r["last_views"] or 0 for r in rows]
    pct = lambda vs: f"{100 * sum(v >= min_paid for v in vs) / len(vs):.0f}%" if vs else "-"  # noqa: E731
    approved = con.execute("SELECT account, SUM(amount_rub), COUNT(*) FROM publications WHERE submit_status = "
                           "'approved' AND decided_at >= ? GROUP BY account ORDER BY 2 DESC", (since,)).fetchall()
    rejected = con.execute("SELECT reject_reason, COUNT(*) FROM publications WHERE submit_status = 'rejected' "
                           "AND decided_at >= ? GROUP BY 1 ORDER BY 2 DESC LIMIT 5", (since,)).fetchall()
    pending = con.execute("SELECT COUNT(*) FROM publications WHERE submit_status IN ('submit_now','submitted')"
                          ).fetchone()[0]
    totals = (f"Итого: роликов {len(rows)}, просмотров {num(sum(views))}, 50k+ {pct(views)}, "
              f"одобрено {num(sum(r[1] for r in approved))} ₽, ждут подачи/решения {pending}")
    sections = []
    for key, label in GROUPS:
        groups = {}
        for r in rows:
            groups.setdefault(r[key] or "-", []).append(r)
        lines = [f"{label}:", f"{'':16} {'шт':>3} {'просм.':>10} {'медиана':>9} {'50k+':>5}"]
        for name, rs in sorted(groups.items(), key=lambda kv: -sum(r["last_views"] or 0 for r in kv[1]))[:10]:
            vs = [r["last_views"] or 0 for r in rs]
            line = f"{str(name)[:16]:16} {len(rs):>3} {num(sum(vs)):>10} {num(statistics.median(vs)):>9} {pct(vs):>5}"
            if key == "caption_lang":
                geo = [r["ru_audience_pct"] for r in rs if r["ru_audience_pct"] is not None]
                line += f"  РФ {sum(geo) / len(geo):.0f}% (n={len(geo)})" if geo else "  РФ - (n=0)"
            lines.append(line)
        sections.append("\n".join(lines))
    money = ["Деньги за неделю:"] + [f"@{a}: {num(s)} ₽ ({c} шт)" for a, s, c in approved]
    money += [f"Отказ ({c}): {reason or '-'}" for reason, c in rejected] or []
    body = sections + ["\n".join(money)]
    head = f"{out[0]}\n{totals}\n"
    text = head + "<pre>" + html.escape("\n\n".join(body)) + "</pre>"
    while len(text) > 4096 and body:  # trim groups, never the totals
        body.pop(0)
        text = head + "<pre>" + html.escape("\n\n".join(body)) + "</pre>"
    return text


def run_report(cfg, con, dry):
    telegram.send(weekly(con, cfg), dry=dry)
