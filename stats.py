"""Отчёт по вовлечённости: считаем по снимкам просмотров/реакций, которые бот делает после публикации."""
from collections import defaultdict
from zoneinfo import ZoneInfo

KIND = {"single": "обычные", "mutual": "ВП", "night": "ночь"}
ROLE = {"warmup": "разогревы", "post": "посты", "reminder": "напоминания"}
MIN_N = 3  # меньше трёх постов в группе выводов не делаем


def _er(rows) -> float:
    v = sum(r["views"] for r in rows)
    return (sum(r["reactions"] + r["forwards"] + r["replies"] for r in rows) / v * 100) if v else 0.0


def _avg(rows, key="views") -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def build_report(rows: list[dict], tz: ZoneInfo, days: int) -> tuple[str, list[str]]:
    """rows: снимки за период (поля: kind снимка sk, views, forwards, reactions, replies, role, media_type, text, sent_at, set_kind).
    Для честного сравнения берём снимок «1 час»: у всех постов он на одном возрасте."""
    first = [r for r in rows if r["sk"] == "1h"]
    if not first:
        return "Пока нет данных: цифры по постам появятся через час после первых публикаций (нужен подключённый аккаунт).", []
    lines = [f"<b>📊 Статистика за {days} дн.</b>", f"Постов с данными: {len(first)}",
             f"Средние просмотры за 1 час: {_avg(first):.0f}", f"Вовлечённость (реакции+пересылки+ответы к просмотрам): {_er(first):.1f}%", ""]
    advice: list[str] = []

    top = sorted(first, key=lambda r: r["views"], reverse=True)[:3]
    lines.append("<b>Лучшие посты по просмотрам (1 час):</b>")
    for r in top:
        t = (r["text"] or "").replace("\n", " ").strip()[:35] or "(без текста)"
        lines.append(f"• {r['views']} · {r['sent_at'].astimezone(tz).strftime('%d.%m %H:%M')} · {t}")
    lines.append("")

    by_hour = defaultdict(list)
    for r in first:
        by_hour[r["sent_at"].astimezone(tz).hour].append(r)
    hours = {h: rs for h, rs in by_hour.items() if len(rs) >= MIN_N}
    if len(hours) >= 2:
        ranked = sorted(hours.items(), key=lambda kv: _avg(kv[1]), reverse=True)
        lines.append("<b>Часы (средние просмотры за 1 час):</b>")
        for h, rs in ranked[:3]:
            lines.append(f"▲ {h:02d}:00 — {_avg(rs):.0f} ({len(rs)} шт.)")
        for h, rs in [x for x in ranked[-2:] if x not in ranked[:3]]:
            lines.append(f"▼ {h:02d}:00 — {_avg(rs):.0f} ({len(rs)} шт.)")
        lines.append("")
        best, worst = ranked[0], ranked[-1]
        if _avg(worst[1]) and _avg(best[1]) / _avg(worst[1]) >= 1.3:
            advice.append(f"Лучшее время для постов около {best[0]:02d}:00: в среднем на {(_avg(best[1]) / _avg(worst[1]) - 1) * 100:.0f}% больше просмотров, чем в {worst[0]:02d}:00.")

    lines.append("<b>По типам:</b>")
    groups = defaultdict(list)
    for r in first:
        groups[(r["set_kind"], r["role"])].append(r)
    for (k, role), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= MIN_N:
            lines.append(f"• {KIND.get(k, k)} / {ROLE.get(role, role)}: {_avg(rs):.0f} просм., вовлеч. {_er(rs):.1f}% ({len(rs)} шт.)")

    with_p = [r for r in first if r["media_type"]]
    no_p = [r for r in first if not r["media_type"]]
    if len(with_p) >= MIN_N and len(no_p) >= MIN_N:
        a, b = _avg(with_p), _avg(no_p)
        lines.append(f"• С фото/медиа: {a:.0f} · без: {b:.0f}")
        if b and abs(a / b - 1) >= 0.2:
            advice.append("Посты с медиа " + ("получают" if a > b else "получают меньше") + f" просмотров: {abs(a / b - 1) * 100:.0f}% разницы." + (" Ставь фото чаще." if a > b else " Попробуй чаще без фото."))
    er_posts = [r for r in first if r["role"] == "post"]
    er_rem = [r for r in first if r["role"] == "reminder"]
    if len(er_posts) >= MIN_N and len(er_rem) >= MIN_N and _avg(er_posts) and _avg(er_rem) / _avg(er_posts) < 0.6:
        advice.append("Напоминания набирают заметно меньше просмотров, чем сами посты: можно сократить их число или усилить текст.")
    if advice:
        lines += ["", "<b>💡 Советы:</b>"] + [f"• {a}" for a in advice]
    return "\n".join(lines), advice
