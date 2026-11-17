"""Daily summary (Task 4.5); the weekly report arrives in Phase 5."""
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
