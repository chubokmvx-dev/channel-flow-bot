"""Расчёт расписания (чистые функции, без Telegram): когда публиковать и когда удалять."""
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

_AUTO = object()


def _custom(it: dict, send_at: datetime):
    ttl = it.get("custom_ttl")
    if ttl is None:
        return _AUTO
    return None if ttl == 0 else send_at + timedelta(seconds=ttl)


def plan_mutual(items: list[dict], start: datetime, gap: int, now: datetime) -> dict:
    """Взаимный пиар: шаги идут цепочкой с паузой gap.
    Напоминание удаляется, когда выходит следующий шаг; пост и разогревы, когда выходит следующий пост.
    items упорядочены; возвращает {id: (send_at, delete_at)}."""
    g = timedelta(seconds=gap)
    send: dict[int, datetime] = {}
    last_sent = None
    for it in items:
        if it["status"] in ("sent", "deleted") and it.get("sent_at"):
            send[it["id"]] = it["sent_at"]
            if last_sent is None or it["sent_at"] > last_sent:
                last_sent = it["sent_at"]
    cursor = max(last_sent + g, now) if last_sent else max(start, now)
    for it in items:
        if it["status"] in ("draft", "pending"):
            send[it["id"]] = cursor
            cursor += g
    ordered = [it for it in items if it["id"] in send]
    out = {}
    for i, it in enumerate(ordered):
        s = send[it["id"]]
        d = _custom(it, s)
        if d is _AUTO:
            if it["role"] == "reminder":
                d = send[ordered[i + 1]["id"]] if i + 1 < len(ordered) else s + g
            else:
                d = next((send[o["id"]] for o in ordered[i + 1:] if o["role"] == "post"), None)
        out[it["id"]] = (s, d)
    return out


def night_deletes(items: list[dict], send: dict, p2_at: datetime, end_at: datetime) -> dict:
    """Удаления ночи: напоминание уходит, когда выходит следующее; конец части 1 в p2_at, конец части 2 в end_at."""
    present = [it for it in items if it["id"] in send]
    out = {}
    for it in present:
        s = send[it["id"]]
        d = _custom(it, s)
        if d is _AUTO:
            part = it.get("part", 1)
            limit = p2_at if part == 1 else end_at
            if it["role"] == "reminder":
                later = sorted(
                    send[o["id"]] for o in present
                    if o["role"] == "reminder" and o.get("part", 1) == part and send[o["id"]] > s
                )
                d = later[0] if later else limit
            else:
                d = limit
        out[it["id"]] = (s, d)
    return out


def plan_night(items: list[dict], start: datetime, gap: int, tz: ZoneInfo):
    """Ночь. Номинал: разогревы+пост, напоминания 21:00/22:00/23:00, в 00:00 пост 2 (часть 1 удаляется),
    напоминания 02:00/04:00/06:00, в 08:00 всё удаляется.
    Если запуск поздний (пост 1 вышел позже номинала), весь график равномерно сжимается между
    «первое напоминание сразу после поста» и 08:00. Возвращает (plan, p2_at, end_at)."""
    g = timedelta(seconds=gap)
    warm = [i for i in items if i["role"] == "warmup"]
    posts = [i for i in items if i["role"] == "post"]
    p1 = next(i for i in posts if i.get("part", 1) == 1)
    p2 = next((i for i in posts if i.get("part") == 2), None)
    r1 = [i for i in items if i["role"] == "reminder" and i.get("part", 1) == 1]
    r2 = [i for i in items if i["role"] == "reminder" and i.get("part") == 2]

    send: dict[int, datetime] = {}
    t = start
    last = start
    for it in warm + [p1]:
        send[it["id"]] = t
        last = t
        t += g
    first_reminder_min = t  # A

    local_last = last.astimezone(tz)
    day = local_last.date()
    if datetime.combine(day, dtime(8, 0), tzinfo=tz) <= last:
        day += timedelta(days=1)

    def at(d, offset_days, hour):
        return datetime.combine(d + timedelta(days=offset_days), dtime(hour, 0), tzinfo=tz)

    while True:
        anchor, end = at(day, -1, 21), at(day, 0, 8)
        if end - max(first_reminder_min, anchor) >= timedelta(hours=2):
            break
        day += timedelta(days=1)

    def m(nom: datetime) -> datetime:
        if first_reminder_min <= anchor:
            return nom
        frac = (nom - anchor) / (end - anchor)
        return first_reminder_min + (end - first_reminder_min) * frac

    for i, it in enumerate(r1):
        send[it["id"]] = m(at(day, -1, 21 + i))
    p2_at = m(at(day, 0, 0))
    if p2:
        send[p2["id"]] = p2_at
    for i, it in enumerate(r2):
        send[it["id"]] = m(at(day, 0, 2 + 2 * i))
    return night_deletes(items, send, p2_at, end), p2_at, end
