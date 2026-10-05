"""Отчёт по вовлечённости и вступлениям по снимкам, которые бот делает после публикации и перед удалением."""
from collections import defaultdict
from zoneinfo import ZoneInfo

KIND = {"single": "обычные", "mutual": "ВП", "night": "ночь"}
ROLE = {"warmup": "разогревы", "post": "посты", "reminder": "напоминания"}
MIN_N = 3  # меньше трёх сообщений в группе выводов не делаем
_PRIO = {"final": 4, "24h": 3, "6h": 2, "1h": 1}


def best_per_item(rows: list[dict]) -> list[dict]:
    """По каждому сообщению берём самый поздний снимок: у короткоживущих это снимок перед удалением."""
    best: dict[int, dict] = {}
    for r in rows:
        cur = best.get(r["item_id"])
        if cur is None or _PRIO.get(r["sk"], 0) > _PRIO.get(cur["sk"], 0):
            best[r["item_id"]] = r
    return list(best.values())


def _er(rows) -> float:
    v = sum(r["views"] for r in rows)
    return (sum(r["reactions"] + r["forwards"] + r["replies"] for r in rows) / v * 100) if v else 0.0


def _avg(rows, key="views") -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def _with_rel(items: list[dict]) -> None:
    """Просмотры относительно среднего своей группы (тип набора + роль): сообщения разной «продолжительности жизни» сравнимы."""
    groups = defaultdict(list)
    for r in items:
        groups[(r["set_kind"], r["role"])].append(r["views"])
    for r in items:
        g = groups[(r["set_kind"], r["role"])]
        mean = sum(g) / len(g)
        r["rel"] = r["views"] / mean if mean else 0.0


def build_report(rows: list[dict], tz: ZoneInfo, days: int) -> tuple[str, list[str]]:
    items = best_per_item(rows)
    if not items:
        return "Пока нет данных: цифры появляются после первых публикаций и перед удалением постов (нужен подключённый аккаунт).", []
    _with_rel(items)
    lines = [f"<b>📊 Статистика за {days} дн.</b>", f"Сообщений с данными: {len(items)}",
             f"Средние просмотры: {_avg(items):.0f}", f"Вовлечённость (реакции+пересылки+ответы к просмотрам): {_er(items):.1f}%", ""]
    advice: list[str] = []

    posts = [r for r in items if r["role"] == "post"] or items
    lines.append("<b>Лучшие посты по просмотрам:</b>")
    for r in sorted(posts, key=lambda r: r["views"], reverse=True)[:3]:
        t = (r["text"] or "").replace("\n", " ").strip()[:35] or "(без текста)"
        lines.append(f"• {r['views']} · {r['sent_at'].astimezone(tz).strftime('%d.%m %H:%M')} · {t}")
    lines.append("")

    by_hour = defaultdict(list)
    for r in items:
        by_hour[r["sent_at"].astimezone(tz).hour].append(r)
    hours = {h: rs for h, rs in by_hour.items() if len(rs) >= MIN_N}
    if len(hours) >= 2:
        ranked = sorted(hours.items(), key=lambda kv: _avg(kv[1], "rel"), reverse=True)
        lines.append("<b>Часы (охват относительно нормы своего типа сообщений, 100% = как обычно):</b>")
        for h, rs in ranked[:3]:
            lines.append(f"▲ {h:02d}:00 — {_avg(rs, 'rel') * 100:.0f}% ({len(rs)} шт.)")
        for h, rs in [x for x in ranked[-2:] if x not in ranked[:3]]:
            lines.append(f"▼ {h:02d}:00 — {_avg(rs, 'rel') * 100:.0f}% ({len(rs)} шт.)")
        lines.append("")
        best, worst = ranked[0], ranked[-1]
        if _avg(worst[1], "rel") and _avg(best[1], "rel") / _avg(worst[1], "rel") >= 1.25:
            advice.append(f"Время около {best[0]:02d}:00 даёт на {(_avg(best[1], 'rel') / _avg(worst[1], 'rel') - 1) * 100:.0f}% больше охвата, чем {worst[0]:02d}:00 (при том же типе сообщений).")

    lines.append("<b>По типам:</b>")
    groups = defaultdict(list)
    for r in items:
        groups[(r["set_kind"], r["role"])].append(r)
    for (k, role), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= MIN_N:
            life = _avg([{"m": max(0.0, (x["taken_at"] - x["sent_at"]).total_seconds() / 60)} for x in rs], "m")
            lines.append(f"• {KIND.get(k, k)} / {ROLE.get(role, role)}: {_avg(rs):.0f} просм., вовлеч. {_er(rs):.1f}% ({len(rs)} шт., живут ~{life:.0f} мин)")

    with_p = [r for r in items if r["media_type"]]
    no_p = [r for r in items if not r["media_type"]]
    if len(with_p) >= MIN_N and len(no_p) >= MIN_N:
        a, b = _avg(with_p, "rel"), _avg(no_p, "rel")
        lines.append(f"• С фото/медиа: {a * 100:.0f}% · без: {b * 100:.0f}% (относительно нормы)")
        if b and abs(a / b - 1) >= 0.2:
            advice.append(("Посты с медиа набирают больше" if a > b else "Посты без медиа набирают больше") + f" ({abs(a / b - 1) * 100:.0f}% разницы)." + (" Ставь фото чаще." if a > b else " Попробуй чаще без фото."))
    pm = [r for r in items if r["role"] == "post" and r["set_kind"] == "mutual"]
    rm = [r for r in items if r["role"] == "reminder" and r["set_kind"] == "mutual"]
    if len(pm) >= MIN_N and len(rm) >= MIN_N and _avg(pm) and _avg(rm) / _avg(pm) < 0.5:
        advice.append("Напоминания собирают меньше половины просмотров поста (учти: они живут всего пару минут). Если они не приводят вступления, их можно сократить.")

    tracked = [r for r in items if r.get("tracked")]
    joins_total = sum(r["joins"] for r in tracked)
    if tracked:
        lines += ["", "<b>🔗 Вступления по кнопкам-приглашениям</b> (по общей ссылке считаются приблизительно: если рядом идут другие посты, вступления могут учитываться в обоих):", f"Отслеживается сообщений: {len(tracked)}, всего вступлений/заявок: {joins_total}"]
        views = sum(r["views"] for r in tracked)
        if views:
            lines.append(f"Конверсия: {joins_total / views * 100:.2f}% от просмотров")
        for r in sorted(tracked, key=lambda r: r["joins"], reverse=True)[:3]:
            t = (r["text"] or "").replace("\n", " ").strip()[:30] or "(без текста)"
            lines.append(f"• {r['joins']} · {r['sent_at'].astimezone(tz).strftime('%d.%m %H:%M')} · {t}")
        tg = defaultdict(list)
        for r in tracked:
            tg[r["sent_at"].astimezone(tz).hour].append(r)
        cr = {h: (sum(x["joins"] for x in rs) / sum(x["views"] for x in rs)) for h, rs in tg.items() if len(rs) >= MIN_N and sum(x["views"] for x in rs) > 0}
        if len(cr) >= 2:
            top = sorted(cr.items(), key=lambda kv: kv[1], reverse=True)
            lines.append("Лучшие часы по конверсии: " + ", ".join(f"{h:02d}:00 ({c * 100:.2f}%)" for h, c in top[:3]))
            if top[-1][1] and top[0][1] / top[-1][1] >= 1.4:
                advice.append(f"Лучше всего в приглашения переходят около {top[0][0]:02d}:00 (в {top[0][1] / top[-1][1]:.1f} раза выше, чем в {top[-1][0]:02d}:00).")
        by_k = defaultdict(list)
        for r in tracked:
            by_k[(r["set_kind"], r["role"])].append(r)
        for (k, role), rs in by_k.items():
            v = sum(x["views"] for x in rs)
            if len(rs) >= MIN_N and v:
                lines.append(f"• {KIND.get(k, k)} / {ROLE.get(role, role)}: {sum(x['joins'] for x in rs)} вступл., {sum(x['joins'] for x in rs) / v * 100:.2f}%")
    if advice:
        lines += ["", "<b>💡 Советы:</b>"] + [f"• {a}" for a in advice]
    return "\n".join(lines), advice
