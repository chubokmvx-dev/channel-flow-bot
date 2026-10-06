import asyncio
import html
import io
import json
import os
import secrets
import logging
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    LinkPreviewOptions,
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    KeyboardButton,
    Message,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
)

import config
import db
import planner
import relink
import stats as stats_mod
import userbot

logging.basicConfig(level=logging.INFO)
TZ = ZoneInfo(config.TIMEZONE)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID, F.chat.type == "private")
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)
ADMIN = config.ADMIN_ID

KIND = {"single": "📝 Обычный пост", "mutual": "🤝 Взаимный пиар", "night": "🌙 Ночь"}
ROLE = {"warmup": "🔥 Разогрев", "post": "📢 Пост", "reminder": "⏰ Напоминание"}
ROLES = ["warmup", "post", "reminder"]
BTN_RE = re.compile(r"^(.+?)\s*(?:\s[-–—]\s|\|)\s*((?:https?|tg)://\S+)$", re.S)

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Создать пост"), KeyboardButton(text="Контент-план"), KeyboardButton(text="Изменить пост")],
        [KeyboardButton(text="Статистика"), KeyboardButton(text="Настройки")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ---------- состояние интерфейса (бот для одного человека) ----------
mode: tuple | None = None          # ожидаемый ввод: ("repl"|"media"|"btn", item_id) | ("ins", item_id, role) | ("time", set_id) | ("channel",)
fwd: dict | None = None             # пересланный пост, из которого создаём набор
view: tuple | None = None          # что показано на «экране»: ("menu",) ("draft", sid) ("list",) ("set", sid) ("item", iid) ("plan",) ("settings",)
scr_id: int | None = None          # id сообщения-экрана
tmp_ids: list[int] = []            # временные сообщения бота, которые убираем после использования


# ---------- утилиты ----------

def now() -> datetime:
    return datetime.now(TZ)


def fmt(dt: datetime | None, with_date: bool = False) -> str:
    if not dt:
        return "—"
    dt = dt.astimezone(TZ)
    if with_date or dt.date() != now().date():
        return dt.strftime("%d.%m %H:%M")
    return dt.strftime("%H:%M")


def plain(text_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text_html or ""))


def snip(it: dict, n: int = 28) -> str:
    t = plain(it["text_html"]).replace("\n", " ").strip()
    if not t:
        t = {"photo": "фото", "video": "видео", "animation": "гиф", "voice": "голосовое", "audio": "аудио", "document": "файл"}.get(it["media_type"], "…")
    return html.escape(t[:n] + ("…" if len(t) > n else ""))


def flags(it: dict) -> str:
    return (" 📎" if it["media_type"] else "") + (" 🔗" if get_btns(it) else "") + (
        " 🗑" if it["custom_ttl"] not in (None,) else "")


def label(it: dict, kind: str) -> str:
    if kind == "night" and it["role"] == "post":
        return f"📢 Пост {it['part']}"
    if kind == "night" and it["role"] == "reminder":
        return f"⏰ Напоминание {it['part']}"
    return ROLE[it["role"]]


async def tmp(bot: Bot, text: str, kb: Kb | None = None) -> Message:
    m = await bot.send_message(ADMIN, text, reply_markup=kb)
    tmp_ids.append(m.message_id)
    return m


async def clear_tmp(bot: Bot) -> None:
    ids = list(tmp_ids)
    tmp_ids.clear()
    for mid in ids:
        try:
            await bot.delete_message(ADMIN, mid)
        except Exception:
            pass


async def gap() -> int:
    return int(await db.get_setting("gap", "120"))


async def start_range() -> tuple[int, int]:
    return int(await db.get_setting("start_min", "120")), int(await db.get_setting("start_max", "300"))


async def channel():
    v = await db.get_setting("channel") or config.CHANNEL_ID
    if not v:
        return None
    return int(v) if str(v).lstrip("-").isdigit() else v


def extract(m: Message) -> dict | None:
    """Достаёт из сообщения текст (с форматированием) и медиа. None, если тип не поддерживается."""
    media_type = file_id = None
    if m.photo:
        media_type, file_id = "photo", m.photo[-1].file_id
    elif m.video:
        media_type, file_id = "video", m.video.file_id
    elif m.animation:
        media_type, file_id = "animation", m.animation.file_id
    elif m.voice:
        media_type, file_id = "voice", m.voice.file_id
    elif m.audio:
        media_type, file_id = "audio", m.audio.file_id
    elif m.document:
        media_type, file_id = "document", m.document.file_id
    elif not m.text:
        return None
    btns = buttons_from_markup(getattr(m, "reply_markup", None))
    d = {"text_html": (m.html_text or "") if (m.text or m.caption) else "", "media_type": media_type, "file_id": file_id,
            "src_chat": m.chat.id, "src_msg": m.message_id,
            "text_msg": m.message_id if (m.text or m.caption) else None,
            "media_msg": m.message_id if media_type else None,
            # ID сообщений в личке у бота и у пользователя разные, поэтому для userbot ищем по времени отправки
            "text_ts": int(m.date.timestamp()) if (m.text or m.caption) else None,
            "media_ts": int(m.date.timestamp()) if media_type else None, "media_url": None}
    raw = m.text if m.text is not None else m.caption
    if raw is not None:  # сохраняем сам текст и его entities (в т.ч. премиум-эмодзи): публикация не зависит от исходного сообщения
        ents = m.entities if m.text is not None else m.caption_entities
        d["plain"] = raw
        d["ents"] = json.dumps([{"type": e.type, "offset": e.offset, "length": e.length, "url": e.url,
                                 "language": e.language, "custom_emoji_id": e.custom_emoji_id} for e in (ents or [])])
    if btns:  # кнопки есть только у пересланных постов; у обычных сообщений не затираем уже заданные
        d["btns"] = json.dumps(btns, ensure_ascii=False)
    return d


STYLE_EMOJI = {"danger": "🔴", "success": "🟢", "primary": "🔵"}
EMOJI_STYLE = {v: k for k, v in STYLE_EMOJI.items()}
MAX_BTNS = 4


def get_btns(it: dict) -> list[dict]:
    """Кнопки-ссылки поста: [{text, url, style}], каждая в своём ряду."""
    if it.get("btns"):
        try:
            return json.loads(it["btns"])
        except ValueError:
            return []
    if it.get("btn_url"):
        return [{"text": it["btn_text"], "url": it["btn_url"], "style": None}]
    return []


def parse_btns(text: str) -> list[dict] | None:
    """Одна строка = одна кнопка: «[🔴|🔵|🟢] Текст - https://ссылка». None, если формат не понят."""
    out = []
    for line in [l.strip() for l in text.splitlines() if l.strip()]:
        style = None
        if line[0] in EMOJI_STYLE:
            style, line = EMOJI_STYLE[line[0]], line[1:].strip()
        mt = BTN_RE.match(line)
        if not mt:
            return None
        out.append({"text": mt.group(1).strip()[:60], "url": mt.group(2), "style": style})
    return out if 0 < len(out) <= MAX_BTNS else None


def buttons_from_markup(rm) -> list[dict]:
    out = []
    for row in getattr(rm, "inline_keyboard", None) or []:
        for b in row:
            if getattr(b, "url", None):
                out.append({"text": b.text[:60], "url": b.url, "style": getattr(b, "style", None)})
    return out[:MAX_BTNS]


def markup(it: dict) -> Kb | None:
    btns = get_btns(it)
    if not btns:
        return None
    return Kb(inline_keyboard=[[Btn(text=b["text"], url=b["url"], **({"style": b["style"]} if b.get("style") else {}))] for b in btns])


HID_RE = re.compile(r'^\s*<a href="[^"]+">\u200b</a>')


def hidden_preview(it: dict) -> bool:
    """Пост начинается со скрытой ссылки (невидимый символ): предпросмотр этой ссылки нужен, он и показывает картинку."""
    return bool(HID_RE.match(it.get("text_html") or ""))


def public_base() -> str | None:
    d = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{d}" if d else (os.environ.get("PUBLIC_URL") or None)


async def host_photo(bot: Bot, data: dict) -> None:
    """Фото выкладываем по своей ссылке: так оно выходит как большой предпросмотр под текстом, а не вложением."""
    data["media_url"] = None
    base = public_base()
    if not base or data.get("media_type") != "photo":
        return
    try:
        buf = io.BytesIO()
        await bot.download(data["file_id"], destination=buf)
        token = secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")
        await db.put_file(token, buf.getvalue(), "image/jpeg")
        data["media_url"] = f"{base}/m/{token}.jpg"
        if userbot.enabled():
            spawn(userbot.ensure_preview(data["media_url"], wait=20))  # заранее просим Telegram скачать картинку: к публикации превью уже готово
    except Exception:
        logging.exception("host_photo failed")


async def send_item(bot: Bot, chat, it: dict) -> list[int]:
    kb, text, mt, fid = markup(it), it["text_html"] or "", it["media_type"], it["file_id"]
    if it.get("media_url") and mt == "photo" and text.strip():
        url = it["media_url"]
        opts = LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=False)
        return [(await bot.send_message(chat, f'<a href="{url}">\u200b</a>' + text, reply_markup=kb, link_preview_options=opts)).message_id]
    if it.get("src_msg") and (mt or hidden_preview(it)):
        # копируем исходное сообщение целиком: так сохраняются премиум-эмодзи и всё форматирование
        try:
            extra = {"show_caption_above_media": True} if mt in ("photo", "video", "animation") and text else {}
            copied = await bot.copy_message(chat, it["src_chat"], it["src_msg"], reply_markup=kb, **extra)
            return [copied.message_id]
        except Exception as e:
            logging.warning("copy_message failed, sending manually: %s", e)
    if not mt:
        opts = LinkPreviewOptions(is_disabled=False, prefer_large_media=True) if hidden_preview(it) else LinkPreviewOptions(is_disabled=True)
        return [(await bot.send_message(chat, text or "…", reply_markup=kb, link_preview_options=opts)).message_id]
    sender = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation, "voice": bot.send_voice, "audio": bot.send_audio,
              "document": bot.send_document}[mt]
    if len(text) <= 1024:
        extra = {"show_caption_above_media": True} if mt in ("photo", "video", "animation") and text else {}
        return [(await sender(chat, **{mt: fid}, caption=text or None, reply_markup=kb, **extra)).message_id]
    first = await bot.send_message(chat, text)
    second = await sender(chat, **{mt: fid}, reply_markup=kb)
    return [first.message_id, second.message_id]


_DUR = re.compile(r"(\d+(?:[.,]\d+)?)\s*([а-яa-z]*)")
_UNIT = (("д", 86400), ("d", 86400), ("ч", 3600), ("h", 3600), ("с", 1), ("s", 1), ("м", 60), ("m", 60))


def parse_duration(text: str) -> int | None:
    """«15» (минут), «15м», «1ч 30м», «2 часа», «1д», «90 сек» → секунды. None, если не понял."""
    t = text.lower().strip()
    found = _DUR.findall(t)
    if not found or _DUR.sub("", t).strip(" ,+и"):
        return None
    total = 0.0
    for num, unit in found:
        mult = 60 if not unit else next((m for u, m in _UNIT if unit.startswith(u)), None)
        if mult is None:
            return None
        total += float(num.replace(",", ".")) * mult
    return int(round(total)) if total >= 1 else None


def fmt_duration(sec: int) -> str:
    parts = []
    for name, n in (("д", 86400), ("ч", 3600), ("мин", 60), ("с", 1)):
        if sec >= n:
            parts.append(f"{sec // n} {name}")
            sec %= n
    return " ".join(parts) or "0 с"


def parse_when(text: str) -> datetime | None:
    t = text.strip()
    n = now()
    for f in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            d = datetime.strptime(t, f)
        except ValueError:
            continue
        if f == "%H:%M":
            d = n.replace(hour=d.hour, minute=d.minute, second=0, microsecond=0)
            return d if d > n else d + timedelta(days=1)
        if f == "%d.%m %H:%M":
            d = d.replace(year=n.year, tzinfo=TZ)
            return d if d >= n - timedelta(minutes=1) else d.replace(year=n.year + 1)
        return d.replace(tzinfo=TZ)
    return None


# ---------- правила наборов ----------

def check_add(kind: str, items: list[dict], role: str) -> tuple[bool, int, str]:
    if kind != "night":
        return True, 1, ""
    posts = [i for i in items if i["role"] == "post"]
    if role == "warmup":
        if posts or sum(1 for i in items if i["role"] == "warmup") >= 2:
            return False, 1, "В ночи разогревов не больше двух, и только перед первым постом."
        return True, 1, ""
    if role == "post":
        if len(posts) >= 2:
            return False, 2, "В ночи только два поста."
        return True, len(posts) + 1, ""
    if not posts:
        return False, 1, "Сначала добавь пост, потом напоминания."
    part = 2 if len(posts) == 2 else 1
    if sum(1 for i in items if i["role"] == "reminder" and i["part"] == part) >= 3:
        return False, part, "В каждой части ночи максимум 3 напоминания."
    return True, part, ""


def next_role_after(kind: str, items: list[dict], role: str) -> str:
    if kind == "single":
        return "post"
    if role == "warmup":
        run = 0
        for it in reversed(items):
            if it["role"] != "warmup":
                break
            run += 1
        return "post" if run >= 2 else "warmup"
    if kind == "night" and role == "reminder":
        posts = sum(1 for i in items if i["role"] == "post")
        n1 = sum(1 for i in items if i["role"] == "reminder" and i["part"] == 1)
        if posts == 1 and n1 >= 3:
            return "post"
    return "reminder"


# ---------- экраны ----------

async def v_stats():
    if not userbot.enabled():
        return "Статистика собирается через твой аккаунт (userbot), а он сейчас не подключён.", Kb(inline_keyboard=[[Btn(text="✖️ Закрыть", callback_data="close")]])
    text, _ = stats_mod.build_report(await db.snap_rows(14), TZ, 14)
    return text, Kb(inline_keyboard=[[Btn(text="🔄 Обновить", callback_data="stats"), Btn(text="✖️ Закрыть", callback_data="close")]])


async def v_menu():
    kb = Kb(inline_keyboard=[
        [Btn(text=KIND["single"], callback_data="new:single")],
        [Btn(text=KIND["mutual"], callback_data="new:mutual")],
        [Btn(text=KIND["night"], callback_data="new:night")],
        [Btn(text="❌ Отмена", callback_data="close")],
    ])
    return "Какой пост создаём?", kb


async def v_draft(sid: int):
    s = await db.get_set(sid)
    if not s:
        return await v_menu()
    items = await db.items_of(sid)
    lines = [f"<b>{KIND[s['kind']]}</b> · черновик", ""]
    for n, it in enumerate(items, 1):
        lines.append(f"{n}. {label(it, s['kind'])}: {snip(it)}{flags(it)}")
    if s["kind"] == "single":
        lines.append("" if items else "Отправь текст или фото с подписью: это будет пост. Новое сообщение заменит текущее.")
    else:
        if not items:
            lines.append("Отправляй сообщения по очереди: разогрев, разогрев, пост, напоминания…")
        nr = s["next_role"]
        posts = sum(1 for i in items if i["role"] == "post")
        nr_label = "📢 Пост 2" if s["kind"] == "night" and nr == "post" and posts == 1 else ROLE[nr]
        lines += ["", f"Следующее сообщение будет: <b>{nr_label}</b>"]
    if s["kind"] == "night":
        lines.append("Ночь: разогревы и пост → напоминания 21:00, 22:00, 23:00 → в 00:00 пост 2 → напоминания 02:00, 04:00, 06:00 → в 08:00 всё удаляется. "
                     "Если запуск поздний, напоминания первой части равномерно сожмутся до 00:00, пост 2 выйдет ровно в 00:00.")
    lo, hi = await start_range()
    if s["start_at"]:
        lines.append(f"🕐 Старт: {fmt(s['start_at'], True)}")
    else:
        lines.append("🕐 Старт: сразу" if s["kind"] == "single" else f"🕐 Старт: через {lo // 60}–{hi // 60} мин после запуска")
    rows = []
    if s["kind"] != "single":
        rows.append([Btn(text=f"Следующее: {nr_label} ⟳", callback_data=f"nc:{sid}")])
    if items:
        last = items[-1]["id"]
        rows.append([Btn(text="✏️ Текст", callback_data=f"tx:{last}"), Btn(text="📎 Медиа", callback_data=f"md:{last}"),
                     Btn(text="🔗 Кнопки", callback_data=f"bt:{last}"), Btn(text="🗑 Таймер", callback_data=f"tm:{last}")])
        extra = [Btn(text="👁 Просмотр", callback_data=f"pv:{last}")]
        if s["kind"] != "single":
            extra.append(Btn(text="↩️ Убрать последнее", callback_data=f"rl:{sid}"))
        rows.append(extra)
    go = ("✅ Запланировать" if s["start_at"] else "🚀 Опубликовать") if s["kind"] == "single" else "🚀 Запустить"
    when = fmt(s["start_at"], True) if s["start_at"] else "сразу"
    rows.append([Btn(text=f"🕐 {when}", callback_data=f"tp:{sid}"),
                 Btn(text="🔁 " + (REPEAT[s["repeat"]] if s["repeat"] else "без повтора"), callback_data=f"rc:{sid}")])
    rows.append([Btn(text=go, callback_data=f"go:{sid}")])
    rows.append([Btn(text="❌ Отмена", callback_data=f"x:{sid}")])
    return "\n".join(lines), Kb(inline_keyboard=rows)


REPEAT = {"daily": "каждый день", "weekly": "каждую неделю", "monthly": "каждый месяц"}


def add_period(dt: datetime, kind: str) -> datetime:
    """Следующее срабатывание в то же местное время суток."""
    dt = dt.astimezone(TZ)
    if kind == "daily":
        return dt + timedelta(days=1)
    if kind == "weekly":
        return dt + timedelta(days=7)
    y, mo = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    return dt.replace(year=y, month=mo, day=min(dt.day, last))


async def v_list():
    sets = await db.active_sets()
    kb = []
    for s in sets:
        if s["status"] == "draft" and not s["n"]:
            continue
        when = "черновик" if s["status"] == "draft" else f"дальше {fmt(s['next_at'])}" if s["next_at"] else "идёт удаление"
        rep = " 🔁" if s["repeat"] else ""
        kb.append([Btn(text=f"{KIND[s['kind']]}{rep} · {s['n']} шт. · {when}", callback_data=f"set:{s['id']}")])
    kb.append([Btn(text="📚 Уже в канале (править текст и кнопки)", callback_data="hist")])
    kb.append([Btn(text="✖️ Закрыть", callback_data="close")])
    return ("Что изменить?" if len(kb) > 2 else "Запланированных наборов нет. Опубликованные посты можно править здесь:"), Kb(inline_keyboard=kb)


async def v_hist():
    rows = await db.recent_sent()
    if not rows:
        return "Опубликованных постов за последние 2 недели нет.", Kb(inline_keyboard=[[Btn(text="◀️ Назад", callback_data="list")]])
    kb = [[Btn(text=f"{fmt(r['sent_at'])} · {label(r, r['kind'])}: {html.unescape(snip(r, 24))}", callback_data=f"it:{r['id']}")] for r in rows]
    kb.append([Btn(text="◀️ Назад", callback_data="list")])
    return "Опубликованные сообщения (последние 2 недели). Выбери, какое поправить:", Kb(inline_keyboard=kb)


async def v_set(sid: int):
    s = await db.get_set(sid)
    if not s or s["status"] != "scheduled":
        return await v_list()
    items = await db.items_of(sid)
    lines = [f"<b>{KIND[s['kind']]}</b>" + (" · ⏸ на паузе" if s["paused"] else "") + (f" · 🔁 {REPEAT[s['repeat']]}" if s["repeat"] else ""), ""]
    nums, n = [], 0
    has_pending = any(i["status"] == "pending" for i in items)
    for it in items:
        if it["status"] == "pending":
            n += 1
            nums.append(Btn(text=str(n), callback_data=f"it:{it['id']}"))
            lines.append(f"{n}. ▫️ {fmt(it['send_at'])} {label(it, s['kind'])}: {snip(it)}{flags(it)}")
        elif it["status"] == "sent":
            d = f", удалится {fmt(it['delete_at'])}" if it["delete_at"] else ""
            if it["delete_at"]:
                n += 1
                nums.append(Btn(text=str(n), callback_data=f"it:{it['id']}"))
            lines.append(f"{str(n) + '. ' if it['delete_at'] else ''}✅ {fmt(it['sent_at'])} {label(it, s['kind'])}: {snip(it)}{d}")
        elif it["status"] == "deleted":
            lines.append(f"▪️ {fmt(it['sent_at'])} {label(it, s['kind'])}: удалено")
        elif it["status"] == "failed":
            lines.append(f"❌ {label(it, s['kind'])}: не опубликовано")
    if nums:
        lines += ["", "Нажми номер, чтобы изменить сообщение:"]
    kb = [nums[i:i + 6] for i in range(0, min(len(nums), 30), 6)]
    ctl = []
    if has_pending:
        if not s["paused"]:
            ctl += [Btn(text="⏩ Сейчас", callback_data=f"nw:{sid}"), Btn(text="⏭ Пропуск", callback_data=f"sk:{sid}")]
        ctl.append(Btn(text="▶️ Дальше" if s["paused"] else "⏸ Пауза", callback_data=f"pz:{sid}"))
    if ctl:
        kb.insert(0, ctl)
    if has_pending and not any(i["status"] in ("sent", "deleted") for i in items):
        kb.insert(1 if ctl else 0, [Btn(text="🕐 Изменить время", callback_data=f"tp:{sid}")])
    kb.append([Btn(text="⏹ Остановить…", callback_data=f"sx:{sid}"), Btn(text="◀️ Назад", callback_data="list")])
    return "\n".join(lines[:60]), Kb(inline_keyboard=kb)


async def v_item(iid: int):
    it = await db.get_item(iid)
    if not it:
        return await v_list()
    s = await db.get_set(it["set_id"])
    lines = [f"<b>{label(it, s['kind'])}</b>", ""]
    t = plain(it["text_html"]).strip()
    lines.append(html.escape(t[:300] + ("…" if len(t) > 300 else "")) or "(без текста)")
    lines.append("")
    if it["media_type"]:
        lines.append("📎 Есть медиа")
    for b in get_btns(it):
        lines.append(f"🔗 {STYLE_EMOJI.get(b.get('style'), '')}{html.escape(b['text'])} → {html.escape(b['url'])}")
    if it["status"] == "pending":
        lines.append(f"Выйдет: {fmt(it['send_at'], True)}")
    else:
        lines.append(f"Опубликовано: {fmt(it['sent_at'], True)}")
    ttl = it["custom_ttl"]
    lines.append("Удаление: " + (f"{fmt(it['delete_at'], True)}" + (" (по таймеру)" if ttl else "") if it["delete_at"] else "не удаляется"))
    rows = []
    if it["status"] == "pending":
        rows.append([Btn(text="✏️ Текст", callback_data=f"tx:{iid}"), Btn(text="📎 Медиа", callback_data=f"md:{iid}"),
                     Btn(text="🔗 Кнопки", callback_data=f"bt:{iid}"), Btn(text="🗑 Таймер", callback_data=f"tm:{iid}")])
        if s["kind"] == "mutual" or (s["kind"] == "night" and s["p2_at"]):
            rows.append([Btn(text="⬆️", callback_data=f"up:{iid}"), Btn(text="⬇️", callback_data=f"dn:{iid}"),
                         Btn(text="➕ Вставить", callback_data=f"im:{iid}")])
        rows.append([Btn(text="🔁 Заменить", callback_data=f"rp:{iid}"), Btn(text="❌ Убрать", callback_data=f"rm:{iid}")])
    else:
        if it["ch_msg_ids"]:
            rows.append([Btn(text="✏️ Текст в канале", callback_data=f"tx:{iid}"), Btn(text="🔗 Кнопки в канале", callback_data=f"bt:{iid}")])
        rows.append([Btn(text="🗑 Таймер", callback_data=f"tm:{iid}"), Btn(text="🗑 Удалить из канала сейчас", callback_data=f"dl:{iid}")])
    back = f"set:{it['set_id']}" if s["status"] == "scheduled" else "hist"
    rows.append([Btn(text="👁 Показать", callback_data=f"pv:{iid}"), Btn(text="◀️ Назад", callback_data=back)])
    return "\n".join(lines), Kb(inline_keyboard=rows)


async def v_plan():
    rows = await db.timeline()
    events = []
    for it in rows:
        label_ = label(it, it["kind"])
        if it["status"] == "pending":
            events.append((it["send_at"], f"➕ {label_}: {snip(it, 22)}"))
        if it["delete_at"]:
            events.append((it["delete_at"], f"🗑 {label_}: {snip(it, 22)}"))
    events.sort(key=lambda e: e[0])
    if not events:
        text = "Контент-план пуст. Нажми «Создать пост»."
    else:
        out, day = ["<b>Контент-план</b>"], None
        for when, line in events[:40]:
            d = when.astimezone(TZ).date()
            if d != day:
                day = d
                out.append(f"\n<b>{d.strftime('%d.%m')}</b>")
            out.append(f"{when.astimezone(TZ).strftime('%H:%M')} {line}")
        text = "\n".join(out)
    kb = Kb(inline_keyboard=[[Btn(text="🔄 Обновить", callback_data="plan"), Btn(text="✏️ Изменить пост", callback_data="list")],
                             [Btn(text="✖️ Закрыть", callback_data="close")]])
    return text, kb


async def v_settings():
    g = await gap()
    lo, hi = await start_range()
    title = await db.get_setting("channel_title") or "не выбран"
    text = (f"<b>Настройки</b>\n\n📣 Канал: {html.escape(title)}\n⏱ Пауза между шагами: {g // 60} мин\n"
            f"⏳ Старт после запуска: {lo // 60}–{hi // 60} мин\n🕐 Часовой пояс: {config.TIMEZONE}\n"
            f"👤 Публикация от твоего аккаунта (премиум-эмодзи): {'включена' if userbot.enabled() else 'выключена'}")
    lm = (await db.get_setting("link_mode", "shared")) == "unique"
    text += "\n🔗 Ссылки-приглашения в кнопках: " + ("своя для каждого сообщения (точный счёт вступлений)" if lm else "ваша ссылка как есть (вступления считаются приблизительно)")
    rx = (await db.get_setting("react_warmup", "1")) == "1"
    text += "\n👍 Реакции на разогревы (👍 ❤ 🔥): " + ("ставятся сами" if rx else "выключены")
    kb = Kb(inline_keyboard=[
        [Btn(text="📣 Выбрать канал", callback_data="cfg:channel")],
        [Btn(text="👍 Реакции: " + ("→ выключить" if rx else "→ включить"), callback_data="cfg:react")],
        [Btn(text="🔗 Ссылки: " + ("→ как есть" if lm else "→ уникальные"), callback_data="cfg:links")],
        [Btn(text=("✓ " if g == v else "") + f"{v // 60} мин", callback_data=f"cfg:gap:{v}") for v in (60, 120, 180, 300)],
        [Btn(text=("✓ " if (lo, hi) == (a, b) else "") + f"{a // 60}–{b // 60} мин", callback_data=f"cfg:start:{a}:{b}")
         for a, b in ((0, 60), (120, 300), (300, 600))],
        [Btn(text="✖️ Закрыть", callback_data="close")],
    ])
    return text, kb


async def build_view():
    v = view
    if v[0] == "menu":
        return await v_menu()
    if v[0] == "draft":
        return await v_draft(v[1])
    if v[0] == "list":
        return await v_list()
    if v[0] == "hist":
        return await v_hist()
    if v[0] == "set":
        return await v_set(v[1])
    if v[0] == "item":
        return await v_item(v[1])
    if v[0] == "plan":
        return await v_plan()
    if v[0] == "stats":
        return await v_stats()
    return await v_settings()


async def delete_screen(bot: Bot) -> None:
    global scr_id
    if scr_id:
        try:
            await bot.delete_message(ADMIN, scr_id)
        except Exception:
            pass
    scr_id = None


async def render(bot: Bot, fresh: bool = False) -> None:
    global scr_id
    if view is None:
        await delete_screen(bot)
        return
    text, kb = await build_view()
    if fresh or not scr_id:
        await delete_screen(bot)
        scr_id = (await bot.send_message(ADMIN, text, reply_markup=kb)).message_id
        return
    try:
        await bot.edit_message_text(text, chat_id=ADMIN, message_id=scr_id, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await delete_screen(bot)
            scr_id = (await bot.send_message(ADMIN, text, reply_markup=kb)).message_id


async def open_view(bot: Bot, v: tuple | None, fresh: bool = False) -> None:
    global view, mode
    mode = None
    await clear_tmp(bot)
    view = v
    await render(bot, fresh)


# ---------- планирование ----------

async def apply_plan(plan: dict, items: list[dict]) -> None:
    for it in items:
        if it["id"] not in plan:
            continue
        send_at, delete_at = plan[it["id"]]
        if it["status"] in ("draft", "pending"):
            await db.upd_item(it["id"], send_at=send_at, delete_at=delete_at, status="pending")
        elif it["status"] == "sent":
            await db.upd_item(it["id"], delete_at=delete_at)


async def night_fix(s: dict, items: list[dict]) -> None:
    """Запущенная ночь: после вставки, удаления или перестановки заново расставляем время ещё не вышедших сообщений."""
    p2_at, end_at = s["p2_at"], s["end_at"]
    if not (p2_at and end_at):
        return
    g = timedelta(seconds=await gap())
    n = now()
    items = sorted(items, key=lambda i: i["pos"])
    r1 = [i for i in items if i["role"] == "reminder" and i.get("part", 1) == 1]
    r2 = [i for i in items if i["role"] == "reminder" and i.get("part") == 2]
    anchor = p2_at - timedelta(hours=3)  # номинал: 21:00, 22:00, 23:00
    # 1) часть 1
    sent1 = [i["sent_at"] for i in items if i["status"] in ("sent", "deleted") and i.get("part", 1) == 1 and i["sent_at"]]
    earliest = planner.ceil_min(max([n + timedelta(seconds=30)] + [t + g for t in sent1]))
    pend1 = [(k, i) for k, i in enumerate(r1) if i["status"] == "pending"]
    nominal = {i["id"]: anchor + timedelta(hours=k) for k, i in pend1}
    if pend1:
        if all(nominal[i["id"]] >= earliest for _, i in pend1):
            times = nominal
        else:
            if p2_at - earliest < g * len(pend1):
                times = None
            else:
                step = (p2_at - earliest) / len(pend1)
                times = {i["id"]: planner.round_min(earliest + step * j) for j, (_, i) in enumerate(pend1)}
        if times:
            for i in sorted((i for _, i in pend1), key=lambda x: times[x["id"]]):
                await db.upd_item(i["id"], send_at=times[i["id"]])
    # 2) пост 2 и часть 2
    for i in items:
        if i["status"] != "pending":
            continue
        if i["role"] == "post" and i.get("part") == 2:
            await db.upd_item(i["id"], send_at=p2_at)
    for k, i in enumerate(r2):
        if i["status"] == "pending":
            await db.upd_item(i["id"], send_at=p2_at + timedelta(hours=2 + 2 * k))


async def replan(sid: int, delay: int = 0) -> None:
    s = await db.get_set(sid)
    if not s or s["status"] != "scheduled" or s["paused"]:
        return
    items = [i for i in await db.items_of(sid) if i["status"] != "failed"]
    if s["kind"] == "mutual":
        plan = planner.plan_mutual(items, s["start_at"] or now(), await gap(), now() + timedelta(seconds=delay))
    elif s["kind"] == "night":
        if not any(i["status"] in ("sent", "deleted") for i in items):
            await launch(None, sid)  # ничего не вышло: пересчитываем всю ночь
            return
        await night_fix(s, items)
        items = [i for i in await db.items_of(sid) if i["status"] != "failed"]
        send = {i["id"]: (i["sent_at"] if i["status"] in ("sent", "deleted") else i["send_at"]) for i in items
                if i["send_at"] or i["sent_at"]}
        plan = planner.night_deletes(items, send, s["p2_at"], s["end_at"])
    else:
        plan = {i["id"]: (i["send_at"], i["send_at"] + timedelta(seconds=i["custom_ttl"]) if i["custom_ttl"] else None)
                for i in items if i["send_at"]}
    await apply_plan(plan, items)


async def launch(bot: Bot, sid: int) -> str | None:
    """Возвращает текст ошибки или None, если запущено."""
    s = await db.get_set(sid)
    await sync_set_link(sid)
    items = await db.items_of(sid)
    if not await channel():
        return "Сначала выбери канал: Настройки → Выбрать канал."
    if not items:
        return "Сначала добавь сообщение."
    n = now()
    lo, hi = await start_range()
    g = await gap()
    start = s["start_at"] if s["start_at"] and s["start_at"] > n else None
    if s["kind"] == "single":
        start = start or n
        ttl = items[0]["custom_ttl"]
        await db.upd_item(items[0]["id"], send_at=start, status="pending",
                          delete_at=start + timedelta(seconds=ttl) if ttl else None)
    elif s["kind"] == "mutual":
        start = planner.ceil_min(start or n + timedelta(seconds=random.randint(lo, hi)))
        await apply_plan(planner.plan_mutual(items, start, g, n), items)
    else:
        if not any(i["role"] == "post" and i["part"] == 1 for i in items):
            return "В ночи нужен хотя бы один пост."
        start = planner.ceil_min(start or n + timedelta(seconds=random.randint(lo, hi)))
        try:
            plan, p2_at, end_at = planner.plan_night(items, start, g, TZ)
        except ValueError as e:
            if str(e) == "cross":
                return "Разогревы и пост 1 не успевают выйти до 00:00, поэтому ночь не запускаю и ничего не публикую. Поставь старт раньше или запусти после 00:00 (тогда это будет следующая ночь)."
            return "До 00:00 слишком мало времени для напоминаний первой части. Запусти раньше или поставь старт после 00:00 (тогда это будет следующая ночь)."
        await apply_plan(plan, items)
        await db.upd_set(sid, p2_at=p2_at, end_at=end_at)
    await db.upd_set(sid, status="scheduled", start_at=start)
    return None


async def summary(sid: int) -> str:
    s = await db.get_set(sid)
    items = await db.items_of(sid)
    lines = [f"✅ {KIND[s['kind']]} запущен. Расписание:"]
    for it in items:
        d = f" → удалится {fmt(it['delete_at'])}" if it["delete_at"] else ""
        lines.append(f"{fmt(it['send_at'])} {label(it, s['kind'])}{d}")
    return "\n".join(lines[:40])


# ---------- воркер ----------

async def notify(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN, text)
    except Exception:
        logging.exception("notify failed")


async def with_tracking(it: dict, replace: bool = True) -> dict:
    """Кнопки-приглашения: считаем вступления. replace=True подставляет уникальные ссылки (нужно до публикации),
    иначе только запоминаем счётчик ваших ссылок (можно в фоне, публикацию не задерживает)."""
    btns = get_btns(it)
    if not btns or not userbot.enabled():
        return it
    unique = (await db.get_setting("link_mode", "shared")) == "unique"
    title = f"бот {it['id']} {now().strftime('%d.%m %H:%M')}"

    async def one(b):
        try:
            return await userbot.track_link(b["url"], title, unique)
        except Exception as e:
            logging.warning("track_link failed: %s", e)
            return None

    res = await asyncio.gather(*[one(b) for b in btns])
    track, out = [], []
    for b, t in zip(btns, res):
        if t:
            track.append({**t, "orig": b["url"]})
            out.append({**b, "url": t["link"]})
        else:
            out.append(b)
    if not track:
        return it
    await db.upd_item(it["id"], track=json.dumps(track))
    return {**it, "btns": json.dumps(out, ensure_ascii=False)} if replace else it


_bg: set = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def publish(bot: Bot, ch, it: dict) -> tuple[list[int], str]:
    unique = (await db.get_setting("link_mode", "shared")) == "unique"
    if unique:
        it = await with_tracking(it, True)  # уникальные ссылки нужны до публикации
    res = await _publish(bot, ch, it)
    if not unique and get_btns(it):
        spawn(with_tracking(it, False))  # ваши ссылки остаются как есть: счётчик запоминаем уже в фоне, пост не ждёт
    return res


async def _publish(bot: Bot, ch, it: dict) -> tuple[list[int], str]:
    """Публикует в канал. Если подключён userbot, то от имени аккаунта владельца (работают премиум-эмодзи)."""
    if userbot.enabled() and (it["text_ts"] or it["media_ts"] or it.get("plain") is not None):
        try:
            t0 = time.monotonic()
            ids = await userbot.send(ch, it["text_ts"], it["media_ts"], plain(it["text_html"]),
                                     it["media_url"] if it["media_type"] == "photo" else None, hidden_preview(it),
                                     it.get("plain"), it.get("ents"))
            t1 = time.monotonic()
            while userbot.WARNINGS:
                await notify(bot, f"⚠️ Пост #{it['id']}: {html.escape(userbot.WARNINGS.pop(0))}. Можно переопубликовать.")
            if get_btns(it):
                try:  # кнопки-ссылки может добавить только бот; ему нужно право «Редактирование сообщений»
                    for attempt in range(5):
                        try:
                            await bot.edit_message_reply_markup(chat_id=ch, message_id=ids[-1], reply_markup=markup(it))
                            break
                        except TelegramBadRequest as e:
                            if attempt == 4 or "not found" not in str(e).lower():
                                raise
                            await asyncio.sleep(0.15)  # бот ещё не «увидел» сообщение аккаунта
                    logging.warning("TIMING item=%s send=%.2fs buttons=%.2fs", it["id"], t1 - t0, time.monotonic() - t1)
                except Exception as e:
                    await notify(bot, f"⚠️ Пост вышел, но кнопку добавить не удалось: {html.escape(str(e))}\n"
                                      "Включи боту в канале право «Редактирование сообщений».")
            if it.get("role") == "warmup" and (await db.get_setting("react_warmup", "1")) == "1":
                spawn(put_reactions(bot, ch, it, ids[0]))
            return ids, "user"
        except Exception as e:
            logging.warning("userbot send failed, fallback to bot: %s", e)
            await notify(bot, f"⚠️ Не вышло опубликовать от твоего аккаунта ({html.escape(str(e))}). Публикую через бота, премиум-эмодзи могут не сохраниться.")
    return await send_item(bot, ch, it), "bot"


async def put_reactions(bot: Bot, ch, it: dict, msg_id: int) -> None:
    """Реакции 👍 ❤ 🔥 на разогрев от вашего аккаунта; в фоне, публикацию не задерживают."""
    try:
        err = await userbot.react(ch, msg_id)
        if err:
            logging.warning("reactions failed item=%s: %s", it["id"], err)
            await notify(bot, f"⚠️ Реакции на разогрев #{it['id']} не поставились ({html.escape(err[:120])}). "
                              "Проверьте, что в канале разрешены реакции 👍 ❤ 🔥.")
        else:
            logging.warning("REACTED item=%s msg=%s", it["id"], msg_id)
    except Exception as e:
        logging.exception("reactions")


async def live_edit(bot: Bot, it: dict, text: bool = False, btns: bool = False) -> str | None:
    """Применяет правку к сообщению, которое уже стоит в канале. Возвращает текст ошибки или None."""
    ch = await channel()
    if not (ch and it.get("ch_msg_ids")):
        return "сообщение в канале не найдено"
    ids = [int(x) for x in it["ch_msg_ids"].split(",")]
    try:
        if text:
            body = it.get("plain") if it.get("plain") is not None else plain(it["text_html"])
            photo_card = it["media_url"] if it["media_type"] == "photo" else None
            if it.get("via") == "user" and userbot.enabled():
                await userbot.edit(ch, ids[0], body, it.get("ents"), photo_card, hidden_preview(it))
            elif it["media_type"] and not photo_card:
                await bot.edit_message_caption(chat_id=ch, message_id=ids[0], caption=it["text_html"] or None, reply_markup=markup(it))
            elif photo_card:
                opts = LinkPreviewOptions(url=photo_card, prefer_large_media=True, show_above_text=False)
                await bot.edit_message_text(f'<a href="{photo_card}">\u200b</a>' + it["text_html"], chat_id=ch, message_id=ids[0],
                                            link_preview_options=opts, reply_markup=markup(it))
            else:
                opts = LinkPreviewOptions(is_disabled=False, prefer_large_media=True) if hidden_preview(it) else LinkPreviewOptions(is_disabled=True)
                await bot.edit_message_text(it["text_html"], chat_id=ch, message_id=ids[0], link_preview_options=opts, reply_markup=markup(it))
        if (text or btns) and (it.get("via") == "user" or btns):
            # правка от аккаунта снимает кнопки бота: возвращаем актуальные (или убираем, если их больше нет)
            for attempt in range(4):
                try:
                    await bot.edit_message_reply_markup(chat_id=ch, message_id=ids[-1], reply_markup=markup(it))
                    break
                except TelegramBadRequest as e:
                    low = str(e).lower()
                    if "not modified" in low:
                        break
                    if attempt == 3 or "not found" not in low:
                        raise
                    await asyncio.sleep(0.3)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return None
        return str(e)
    except Exception as e:
        logging.exception("live_edit")
        return str(e)
    logging.warning("EDITED item=%s text=%s btns=%s", it["id"], text, btns)
    return None


_snap_fail: dict[tuple, int] = {}
_last_sweep = [0.0]


async def snapshot(ch, it: dict, kind: str) -> None:
    key = (it["id"], kind)
    if not (userbot.enabled() and ch and it.get("ch_msg_ids")) or _snap_fail.get(key, 0) >= 3:
        return
    try:
        track = json.loads(it["track"]) if it.get("track") else None
        st = await userbot.stats(ch, int(str(it["ch_msg_ids"]).split(",")[0]), track)
        if st:
            await db.save_snap(it["id"], kind, st["views"], st["forwards"], st["reactions"], st["replies"], st["joins"])
        else:  # пост уже удалён вручную: отмечаем, чтобы не опрашивать снова
            await db.save_snap(it["id"], kind, -1, 0, 0, 0)
    except Exception as e:
        _snap_fail[key] = _snap_fail.get(key, 0) + 1
        logging.warning("snapshot failed: %s", e)
        if _snap_fail[key] >= 3:
            await db.save_snap(it["id"], kind, -1, 0, 0, 0)


async def delete_item(bot: Bot, ch, it: dict) -> None:
    await snapshot_final(ch, it)  # быстрый снимок просмотров перед удалением, остальное (вступления, закрытие ссылок) в фоне
    try:
        if ch and it["ch_msg_ids"]:
            ids = [int(x) for x in it["ch_msg_ids"].split(",")]
            if it.get("via") == "user" and userbot.enabled():
                try:
                    await userbot.delete(ch, ids)
                except Exception as e:
                    logging.warning("userbot delete failed: %s", e)
            else:
                for mid in ids:
                    try:
                        await bot.delete_message(ch, mid)
                    except Exception as e:
                        logging.warning("delete failed: %s", e)
    finally:
        logging.warning("DELETED item=%s set=%s role=%s ids=%s", it["id"], it["set_id"], it["role"], it["ch_msg_ids"])
        await db.upd_item(it["id"], status="deleted", delete_at=None)


async def tick(bot: Bot) -> None:
    ch = await channel()
    if ch:  # сначала публикации: новый шаг не должен ждать удалений
        for it in await db.due_sends():
            try:
                ids, via = await publish(bot, ch, it)
                await db.mark_sent(it["id"], ids, via)
                logging.warning("PUBLISHED item=%s set=%s role=%s ids=%s via=%s", it["id"], it["set_id"], it["role"], ids, via)
                s_ = await db.get_set(it["set_id"])
                if s_ and s_["kind"] == "mutual":
                    await replan(it["set_id"])
            except Exception as e:
                await db.upd_item(it["id"], status="failed")
                await notify(bot, f"❌ Не удалось опубликовать «{html.escape(plain(it['text_html'])[:30])}»: {html.escape(str(e))}\n"
                                  "Проверь, что бот админ канала с правом публиковать сообщения.")
    due = await db.due_deletions()
    if due:
        await asyncio.gather(*[delete_item(bot, ch, it) for it in due], return_exceptions=True)
    for sid in await db.unfinished_sets():
        await schedule_repeat(bot, sid)  # сначала следующий повтор, потом закрываем этот: при сбое не потеряем серию
        await db.upd_set(sid, status="done")  # без сообщения: оно поднимало чат с ботом выше канала


async def schedule_repeat(bot: Bot, sid: int) -> None:
    """Набор с повтором: ставим следующий такой же на следующий период в то же время."""
    s = await db.get_set(sid)
    if not s or not s["repeat"] or not s["start_at"]:
        return
    nxt = add_period(s["start_at"], s["repeat"])
    guard = 0
    while nxt <= now() and guard < 400:  # бот долго не работал: пропускаем прошедшие даты
        nxt = add_period(nxt, s["repeat"])
        guard += 1
    new_id = await db.clone_set(sid, nxt)
    err = await launch(bot, new_id)
    if err:
        await db.upd_set(new_id, status="cancelled")
        await notify(bot, f"⚠️ Повтор «{KIND[s['kind']]}» на {fmt(nxt, True)} не поставлен: {html.escape(err)}")


async def snapshot_final(ch, it: dict) -> None:
    if not (userbot.enabled() and ch and it.get("ch_msg_ids")):
        return
    try:
        st = await asyncio.wait_for(userbot.stats(ch, int(str(it["ch_msg_ids"]).split(",")[0]), None), 5)
    except Exception as e:
        logging.warning("final snapshot failed: %s", e)
        st = None
    spawn(finish_final(it, st))


async def finish_final(it: dict, st: dict | None) -> None:
    track = json.loads(it["track"]) if it.get("track") else None
    try:
        j = await userbot.joins(track) if track else 0
    except Exception as e:
        logging.info("joins failed: %s", e)
        j = 0
    if st:
        await db.save_snap(it["id"], "final", st["views"], st["forwards"], st["reactions"], st["replies"], j)
    if track:
        await userbot.revoke(track)  # уникальные ссылки закрываем: у админа лимит активных приглашений (общие не трогаем)
        await db.upd_item(it["id"], track_done=True)


async def keepalive(bot: Bot) -> None:
    """Держит соединение бота с Telegram тёплым, чтобы правка кнопки не тратила время на новое подключение."""
    while True:
        try:
            await bot.get_me()
        except Exception:
            pass
        await asyncio.sleep(8)


async def snap_worker(bot: Bot) -> None:
    """Снимки через час/6 часов/сутки и уборка ссылок живут отдельно от публикаций: не задерживают их."""
    last_sweep = 0.0
    while True:
        try:
            ch = await channel()
            if userbot.enabled() and ch:
                for it in await db.due_snaps():
                    await snapshot(ch, it, it["kind"])
                if time.time() - last_sweep > 600:
                    last_sweep = time.time()
                    for it in await db.stale_tracks():
                        await userbot.revoke(json.loads(it["track"]))
                        await db.upd_item(it["id"], track_done=True)
        except Exception:
            logging.exception("snap_worker failed")
        await asyncio.sleep(20)


async def worker(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            logging.exception("tick failed")
        await asyncio.sleep(1)


# ---------- команды и кнопки нижней клавиатуры ----------

HELP = (
    "Привет! Нижние кнопки: «Создать пост», «Контент-план», «Изменить пост», «Настройки».\n\n"
    "Сначала зайди в «Настройки» и выбери канал (бот должен быть админом канала с правом публиковать и удалять сообщения)."
)


@router.message(Command("start", "help"))
async def cmd_start(m: Message, bot: Bot):
    await m.answer(HELP, reply_markup=MAIN_KB)


@router.message(F.text == "Создать пост")
async def btn_new(m: Message, bot: Bot):
    await db.drop_empty_drafts()
    await open_view(bot, ("menu",), fresh=True)


@router.message(F.text == "Контент-план")
async def btn_plan(m: Message, bot: Bot):
    await open_view(bot, ("plan",), fresh=True)


@router.message(F.text == "Изменить пост")
async def btn_edit(m: Message, bot: Bot):
    await open_view(bot, ("list",), fresh=True)


@router.message(F.text == "Статистика")
async def btn_stats(m: Message, bot: Bot):
    await open_view(bot, ("stats",), fresh=True)


@router.message(F.text == "Настройки")
async def btn_settings(m: Message, bot: Bot):
    await open_view(bot, ("settings",), fresh=True)


async def set_channel(m: Message, bot: Bot, value) -> None:
    global mode
    try:
        chat = await bot.get_chat(value)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await tmp(bot, f"Канал «{html.escape(chat.title or "")}» найден, но бот не админ. Добавь его с правом публиковать и удалять сообщения и пришли пост ещё раз.")
            return
        if member.status == "administrator" and not (getattr(member, "can_post_messages", True) and getattr(member, "can_delete_messages", True)):
            await tmp(bot, "Боту не хватает прав: нужны «Публикация сообщений» и «Удаление сообщений». Включи их и пришли пост ещё раз.")
            return
    except Exception as e:
        await tmp(bot, f"Не удалось получить канал: {html.escape(str(e))}\nДобавь бота админом и попробуй ещё раз.")
        return
    await db.set_setting("channel", chat.id)
    await db.set_setting("channel_title", chat.title or str(chat.id))
    mode = None
    await clear_tmp(bot)
    await render(bot)


# ---------- ввод пользователя ----------

async def add_to_draft(bot: Bot, s: dict, data: dict, after_id: int | None = None, role: str | None = None) -> None:
    await _add_to_draft(bot, s, data, after_id, role)
    await sync_set_link(s["id"])


async def _add_to_draft(bot: Bot, s: dict, data: dict, after_id: int | None = None, role: str | None = None) -> None:
    items = await db.items_of(s["id"])
    if s["kind"] == "single":
        if items:
            await db.upd_item(items[0]["id"], **data)
        else:
            await db.add_item(s["id"], "post", 1, data, "draft")
        return
    role = role or s["next_role"]
    ok, part, msg = check_add(s["kind"], items, role)
    if not ok:
        await tmp(bot, "⚠️ " + msg)
        return
    if s["kind"] == "night" and s["status"] != "draft" and after_id and role == "reminder":
        anc = next((i for i in items if i["id"] == after_id), None)
        part = (anc.get("part") or 1) if anc else part
        if sum(1 for i in items if i["role"] == "reminder" and i.get("part", 1) == part) >= 3:
            await tmp(bot, "⚠️ В каждой части ночи максимум 3 напоминания.")
            return
    if s["kind"] == "night" and s["status"] != "draft" and role == "post" and s["p2_at"] and s["p2_at"] <= now():
        await tmp(bot, "⚠️ Пост 2 выходит в 00:00, это время уже прошло.")
        return
    status = "draft" if s["status"] == "draft" else "pending"
    iid = await db.add_item(s["id"], role, part, data, status, after_id)
    if s["status"] == "draft":
        items = await db.items_of(s["id"])
        await db.upd_set(s["id"], next_role=next_role_after(s["kind"], items, role))
    else:
        await replan(s["id"])


@router.message()
async def on_message(m: Message, bot: Bot):
    global mode, view
    cur = mode

    if cur and cur[0] == "channel":
        if isinstance(m.forward_origin, MessageOriginChannel):
            await set_channel(m, bot, m.forward_origin.chat.id)
        elif m.text:
            t = m.text.strip()
            await set_channel(m, bot, int(t) if t.lstrip("-").isdigit() else t)
        return

    if cur and cur[0] == "time":
        when = parse_when(m.text or "")
        if not when:
            await tmp(bot, "Не понял время. Примеры: 21:30 или 05.10 08:00")
            return
        await apply_time(bot, cur[1], when, fresh=True)
        return

    if cur and cur[0] == "ttl":
        sec = parse_duration(m.text or "")
        if not sec:
            await tmp(bot, "Не понял время. Примеры: 15 (минуты), 45м, 2ч, 1ч 30м, 1д.")
            return
        it = await db.get_item(cur[1])
        await db.upd_item(cur[1], custom_ttl=sec)
        if it and it["status"] == "sent":
            await db.upd_item(cur[1], delete_at=it["sent_at"] + timedelta(seconds=sec))
        await after_item_change(bot, cur[1], fresh=True)
        return

    if cur and cur[0] == "btn":
        t = (m.text or "").strip()
        if t.lower() in ("убрать", "-", "удалить"):
            await db.upd_item(cur[1], btn_text=None, btn_url=None, btns=None)
        elif (bl := parse_btns(t)):
            await db.upd_item(cur[1], btn_text=None, btn_url=None, btns=json.dumps(bl, ensure_ascii=False))
        else:
            await tmp(bot, f"Не понял. Каждая кнопка с новой строки, до {MAX_BTNS} штук:\nТекст - https://ссылка\nЦвет можно задать значком в начале строки: 🔴 красная, 🔵 синяя, 🟢 зелёная.")
            return
        await after_item_change(bot, cur[1], fresh=True)
        it2 = await db.get_item(cur[1])
        if it2 and it2["status"] == "sent":
            err = await live_edit(bot, it2, btns=True)
            await tmp(bot, "✅ Кнопки изменены в канале." if not err else f"⚠️ В канале не изменилось: {html.escape(err[:200])}")
        return

    if cur and cur[0] in ("media", "repl", "ins", "txt"):
        data = extract(m)
        if data is None:
            await tmp(bot, "Этот тип сообщения не поддерживается. Отправь текст, фото, видео, гиф или файл.")
            return
        if cur[0] == "media":
            await host_photo(bot, data)  # карточка только когда фото докрепляют к готовому тексту
            if not data["media_type"]:
                await tmp(bot, "Нужно фото, видео, гиф или файл.")
                return
            await db.upd_item(cur[1], media_type=data["media_type"], file_id=data["file_id"],
                              media_msg=data["media_msg"], media_ts=data["media_ts"], src_msg=None,
                              media_url=data["media_url"])
            await after_item_change(bot, cur[1], fresh=True)
        elif cur[0] == "repl":
            await db.upd_item(cur[1], **data)
            await after_item_change(bot, cur[1], fresh=True)
        elif cur[0] == "txt":
            it = await db.get_item(cur[1])
            if it and it["status"] == "sent" and data["media_type"]:
                await tmp(bot, "У опубликованного поста можно менять только текст. Отправь текстовое сообщение.")
                return
            if data["media_type"] or not (it and it["media_type"]):
                # прислали фото с подписью или у поста нет медиа: берём сообщение целиком
                await db.upd_item(cur[1], **data)
            else:
                # у поста есть медиа: меняем только текст, медиа остаётся
                await db.upd_item(cur[1], text_html=data["text_html"], text_msg=data["text_msg"], text_ts=data["text_ts"], src_msg=None,
                                  plain=data.get("plain"), ents=data.get("ents"))
            await after_item_change(bot, cur[1], fresh=True)
            it2 = await db.get_item(cur[1])
            if it2 and it2["status"] == "sent":
                err = await live_edit(bot, it2, text=True)
                await tmp(bot, "✅ Текст изменён в канале." if not err else f"⚠️ В канале не изменилось: {html.escape(err[:200])}")
        else:
            it = await db.get_item(cur[1])
            s = await db.get_set(it["set_id"])
            await add_to_draft(bot, s, data, after_id=cur[1], role=cur[2])
            mode = None
            await clear_tmp(bot)
            await render(bot, fresh=True)
        return

    # пересланный пост (например, от партнёра) вне черновика: предлагаем сделать из него набор
    if m.forward_origin and not (view and view[0] == "draft"):
        data = extract(m)
        if data is None:
            await tmp(bot, "Этот тип сообщения не поддерживается. Отправь текст, фото, видео, гиф или файл.")
            return
        global fwd
        fwd = data
        await clear_tmp(bot)
        await tmp(bot, "Что сделать из этого поста?", Kb(inline_keyboard=[
            [Btn(text="📝 Обычный пост", callback_data="fw:single")],
            [Btn(text="🤝 Взаимный пиар", callback_data="fw:mutual")],
            [Btn(text="🌙 Ночь", callback_data="fw:night")],
            [Btn(text="Отмена", callback_data="cancel")]]))
        return

    # обычное сообщение: добавляем в открытый черновик
    if view and view[0] == "draft":
        s = await db.get_set(view[1])
        data = extract(m)
        if s and data:
            await clear_tmp(bot)
            await add_to_draft(bot, s, data)
            await render(bot, fresh=True)   # панель всегда под последним сообщением
            return
        await tmp(bot, "Этот тип сообщения не поддерживается. Отправь текст, фото, видео, гиф или файл.")
        return
    await tmp(bot, "Нажми «Создать пост» внизу, чтобы начать.")


async def sync_set_link(sid: int) -> int:
    """Ссылка набора = первая ссылка-приглашение из его первого сообщения, где она есть.
    Во всех остальных ещё не вышедших сообщениях набора любые другие ссылки-приглашения заменяются на неё:
    в тексте, в скрытых ссылках под словом и в кнопках (текст и адрес), форматирование сохраняется."""
    items = await db.items_of(sid)
    main = None
    for it in items:
        links = relink.find_links(it)
        if links:
            main = relink.first_link(it)
            break
    if not main:
        return 0
    n = 0
    for it in items:
        if it["status"] not in ("draft", "pending"):
            continue
        olds = [("https://" + l) for l in relink.find_links(it) if relink.core(l) != relink.core(main)]
        upd = relink.relink_fields(it, olds, main) if olds else {}
        if upd:
            await db.upd_item(it["id"], **upd)
            n += 1
    return n


async def after_item_change(bot: Bot, item_id: int, fresh: bool = False) -> None:
    global mode
    mode = None
    await clear_tmp(bot)
    it = await db.get_item(item_id)
    if it:
        await sync_set_link(it["set_id"])
        s = await db.get_set(it["set_id"])
        if s and s["status"] == "scheduled":
            await replan(s["id"])
    await render(bot, fresh)


async def apply_time(bot: Bot, sid: int, when, fresh: bool = False) -> None:
    """Новое время старта. У уже запущенного набора (пока ничего не вышло) расписание пересчитывается сразу."""
    await db.upd_set(sid, start_at=when)
    s = await db.get_set(sid)
    err = None
    if s and s["status"] == "scheduled":
        err = await launch(bot, sid)
    await finish_set_time(bot, sid, fresh)
    if err:
        await tmp(bot, "⚠️ " + err)


async def finish_set_time(bot: Bot, sid: int, fresh: bool = False) -> None:
    global mode
    mode = None
    await clear_tmp(bot)
    await render(bot, fresh)


# ---------- кнопки ----------

def quick_times() -> list[tuple[str, int]]:
    n = now().replace(second=0, microsecond=0)
    out = [("через 15 мин", n + timedelta(minutes=15)), ("через 30 мин", n + timedelta(minutes=30)),
           ("через 1 час", n + timedelta(hours=1)), ("через 2 часа", n + timedelta(hours=2))]
    h = n.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for i in range(8):
        t = h + timedelta(hours=i)
        out.append((t.strftime("%H:%M"), t))
    return [(a, int(b.timestamp())) for a, b in out]


@router.callback_query()
async def on_cb(c: CallbackQuery, bot: Bot):
    global mode, view
    data = c.data or ""
    action, _, rest = data.partition(":")
    parts = rest.split(":") if rest else []

    if action == "noop":
        await c.answer()
        return
    if action == "close":
        await open_view(bot, None)
        await c.answer()
        return
    if action == "stats":
        await render(bot)
        await c.answer("Обновлено")
        return
    if action in ("plan", "list", "hist"):
        await open_view(bot, (action,))
        await c.answer()
        return
    if action == "fw":
        global fwd
        data, fwd = fwd, None
        if not data:
            await c.answer("Перешли пост ещё раз", show_alert=True)
            return
        await db.drop_empty_drafts()
        sid = await db.new_set(parts[0])
        await add_to_draft(bot, await db.get_set(sid), data, role="post")
        await open_view(bot, ("draft", sid), fresh=True)
        await c.answer()
        return
    if action == "new":
        await db.drop_empty_drafts()
        sid = await db.new_set(parts[0])
        await open_view(bot, ("draft", sid))
        await c.answer()
        return
    if action == "cfg":
        if parts[0] == "channel":
            mode = ("channel",)
            await tmp(bot, "Перешли мне любой пост из канала (или отправь @username / ID канала). Бот должен быть админом канала.",
                      Kb(inline_keyboard=[[Btn(text="Отмена", callback_data="cancel")]]))
        elif parts[0] == "links":
            cur = await db.get_setting("link_mode", "shared")
            await db.set_setting("link_mode", "shared" if cur == "unique" else "unique")
            await render(bot)
        elif parts[0] == "react":
            cur = await db.get_setting("react_warmup", "1")
            await db.set_setting("react_warmup", "0" if cur == "1" else "1")
            await render(bot)
        elif parts[0] == "gap":
            await db.set_setting("gap", parts[1])
            await render(bot)
        elif parts[0] == "start":
            await db.set_setting("start_min", parts[1])
            await db.set_setting("start_max", parts[2])
            await render(bot)
        await c.answer()
        return
    if action == "cancel":
        mode = None
        await clear_tmp(bot)
        await c.answer()
        return

    if action == "nr":
        await db.upd_set(int(parts[0]), next_role=parts[1])
        await render(bot)
        await c.answer()
        return
    if action == "rl":
        items = await db.items_of(int(parts[0]))
        if items:
            await db.del_item(items[-1]["id"])
            s = await db.get_set(int(parts[0]))
            items = await db.items_of(s["id"])
            await db.upd_set(s["id"], next_role=next_role_after(s["kind"], items, items[-1]["role"]) if items else "warmup")
        await render(bot)
        await c.answer()
        return
    if action == "x":
        sid = int(parts[0])
        await db.delete_set(sid)
        await open_view(bot, None)
        await c.answer("Отменено")
        return
    if action == "nc":
        sid = int(parts[0])
        cur = (await db.get_set(sid))["next_role"]
        await db.upd_set(sid, next_role=ROLES[(ROLES.index(cur) + 1) % len(ROLES)])
        await render(bot)
        await c.answer()
        return
    if action == "rc":
        sid = int(parts[0])
        cur = (await db.get_set(sid))["repeat"]
        order = [None, "daily", "weekly", "monthly"]
        await db.upd_set(sid, repeat=order[(order.index(cur) + 1) % 4])
        await render(bot)
        await c.answer()
        return
    if action == "rep":
        sid, k = int(parts[0]), parts[1]
        await db.upd_set(sid, repeat=None if k == "none" else k)
        await render(bot)
        await c.answer()
        return
    if action == "tp":
        sid = int(parts[0])
        s = await db.get_set(sid)
        mode = ("time", sid)
        await clear_tmp(bot)
        qt = quick_times()
        auto = "Сейчас" if s["kind"] == "single" else "Авто"
        rows = [[Btn(text=auto, callback_data=f"ts:{sid}:0")]]
        for i in range(0, len(qt), 4):
            rows.append([Btn(text=a, callback_data=f"ts:{sid}:{b}") for a, b in qt[i:i + 4]])
        rows.append([Btn(text="Отмена", callback_data="cancel")])
        await tmp(bot, "Когда стартуем? Выбери время кнопкой или напиши, например: 21:30 или 05.10 08:00", Kb(inline_keyboard=rows))
        await c.answer()
        return
    if action == "ts":
        sid, ts = int(parts[0]), int(parts[1])
        await apply_time(bot, sid, datetime.fromtimestamp(ts, TZ) if ts else None)
        await c.answer()
        return
    if action == "go":
        sid = int(parts[0])
        err = await launch(bot, sid)
        if err:
            await c.answer(err, show_alert=True)
            return
        text = await summary(sid)
        await open_view(bot, None)
        await bot.send_message(ADMIN, text)
        await c.answer()
        return
    if action == "set":
        s = await db.get_set(int(parts[0]))
        if s and s["status"] == "draft":
            await open_view(bot, ("draft", s["id"]))
        else:
            await open_view(bot, ("set", int(parts[0])))
        await c.answer()
        return
    if action in ("nw", "sk", "pz"):
        sid = int(parts[0])
        s = await db.get_set(sid)
        if not s or s["status"] != "scheduled":
            await c.answer("Набор уже завершён", show_alert=True)
            return
        pend = [i for i in await db.items_of(sid) if i["status"] == "pending"]
        if action == "pz":
            if s["paused"]:
                g = await gap()
                n = now()
                if s["kind"] == "mutual":
                    await db.upd_set(sid, paused=False)
                    await replan(sid, delay=g)
                else:  # ночь: просроченные шаги выходят по очереди, остальные по плану
                    cur = n + timedelta(seconds=g)
                    for i in pend:
                        if i["send_at"] and i["send_at"] < cur:
                            await db.upd_item(i["id"], send_at=cur)
                            cur += timedelta(seconds=g)
                    await db.upd_set(sid, paused=False)
                    await replan(sid)
                await c.answer("Продолжаю")
            else:
                await db.upd_set(sid, paused=True)
                for i in await db.items_of(sid):  # на паузе ничего не удаляется
                    if i["status"] == "sent" and i["delete_at"]:
                        await db.upd_item(i["id"], delete_at=None)
                await c.answer("Пауза")
        elif not pend:
            await c.answer("Больше нечего публиковать", show_alert=True)
            return
        elif action == "nw":
            if s["paused"]:
                await c.answer("Сначала сними паузу", show_alert=True)
                return
            await db.upd_item(pend[0]["id"], send_at=now())
            await c.answer("Публикую")
        else:
            await db.del_item(pend[0]["id"])
            await replan(sid)
            await c.answer("Шаг пропущен")
        await open_view(bot, ("set", sid))
        return
    if action == "im":
        await clear_tmp(bot)
        anchor = await db.get_item(int(parts[0]))
        sd = await db.get_set(anchor["set_id"]) if anchor else None
        roles = ["reminder", "post"] if sd and sd["kind"] == "night" else ROLES
        await tmp(bot, "Какое сообщение вставить сразу после этого?", Kb(inline_keyboard=[
            [Btn(text=("📢 Пост 2" if sd and sd["kind"] == "night" and r == "post" else ROLE[r]), callback_data=f"in:{parts[0]}:{r}") for r in roles],
            [Btn(text="Отмена", callback_data="cancel")]]))
        await c.answer()
        return
    if action == "sx":
        sid = int(parts[0])
        await clear_tmp(bot)
        await tmp(bot, "Остановить набор. Что сделать с тем, что уже вышло в канал?", Kb(inline_keyboard=[
            [Btn(text="⏹ Остановить, оставить в канале", callback_data=f"stop:{sid}")],
            [Btn(text="⏹🗑 Остановить и удалить из канала", callback_data=f"stopdel:{sid}")],
            [Btn(text="Отмена", callback_data="cancel")]]))
        await c.answer()
        return
    if action in ("stop", "stopdel"):
        sid = int(parts[0])
        items = await db.items_of(sid)
        for it in items:
            if it["status"] in ("draft", "pending"):
                await db.del_item(it["id"])
            elif it["status"] == "sent":
                await db.upd_item(it["id"], delete_at=now() if action == "stopdel" else None)
        await db.upd_set(sid, status="cancelled")
        await clear_tmp(bot)
        await open_view(bot, ("list",))
        await c.answer("Остановлено")
        return
    if action == "it":
        await open_view(bot, ("item", int(parts[0])))
        await c.answer()
        return

    # дальше действия над конкретным элементом
    if action in ("md", "tx", "bt", "tm", "pv", "rp", "rm", "up", "dn", "in", "dl", "tt", "mx", "tc"):
        iid = int(parts[0])
        it = await db.get_item(iid)
        if not it:
            await c.answer("Этого элемента уже нет", show_alert=True)
            return
        cancel_kb = Kb(inline_keyboard=[[Btn(text="Отмена", callback_data="cancel")]])
        if action == "md":
            await clear_tmp(bot)
            mode = ("media", iid)
            rows = [[Btn(text="Отмена", callback_data="cancel")]]
            if it["media_type"]:
                rows.insert(0, [Btn(text="🗑 Убрать медиа", callback_data=f"mx:{iid}")])
            await tmp(bot, "Отправь фото, видео, гиф или файл.", Kb(inline_keyboard=rows))
        elif action == "tx":
            await clear_tmp(bot)
            mode = ("txt", iid)
            await tmp(bot, "Отправь новый текст (форматирование и эмодзи сохранятся). Медиа останется на месте.", cancel_kb)
        elif action == "mx":
            await db.upd_item(iid, media_type=None, file_id=None, media_msg=None, media_ts=None, src_msg=None, media_url=None)
            await after_item_change(bot, iid)
        elif action == "bt":
            await clear_tmp(bot)
            mode = ("btn", iid)
            await tmp(bot, f"Отправь кнопки: каждая с новой строки, до {MAX_BTNS} штук.\nТекст - https://ссылка\nЦвет задаётся значком в начале строки: 🔴 красная, 🔵 синяя, 🟢 зелёная (без значка обычная).\nПример:\n🔴 ЗАБРАТЬ ЭКСПРЕСС - https://t.me/...\n🔵 БЕСПЛАТНО - https://t.me/...\nИли слово «убрать».", cancel_kb)
        elif action == "rp":
            await clear_tmp(bot)
            mode = ("repl", iid)
            await tmp(bot, "Отправь новое сообщение (текст или фото с подписью): оно заменит это.", cancel_kb)
        elif action == "tm":
            await clear_tmp(bot)
            def tb(n, v):
                return Btn(text=n, callback_data=f"tt:{iid}:{v}")
            grid = [[tb("Авто (по правилам набора)", "a")],
                    [tb("2 мин", "120"), tb("5 мин", "300"), tb("10 мин", "600"), tb("30 мин", "1800")],
                    [tb("1 час", "3600"), tb("3 часа", "10800"), tb("24 часа", "86400")],
                    [Btn(text="✍️ Своё время", callback_data=f"tc:{iid}"), tb("Не удалять", "0")],
                    [Btn(text="Отмена", callback_data="cancel")]]
            await tmp(bot, "Через сколько после публикации удалить это из канала?", Kb(inline_keyboard=grid))
        elif action == "tc":
            await clear_tmp(bot)
            mode = ("ttl", iid)
            await tmp(bot, "Впиши, через сколько удалить после публикации. Примеры: 15 (это минуты), 45м, 2ч, 1ч 30м, 1д, 90с.", cancel_kb)
        elif action == "tt":
            v = parts[1]
            await db.upd_item(iid, custom_ttl=None if v == "a" else int(v))
            if v != "a" and it["status"] == "sent" and int(v) > 0:
                await db.upd_item(iid, delete_at=it["sent_at"] + timedelta(seconds=int(v)))
            elif v == "0":
                await db.upd_item(iid, delete_at=None)
            await after_item_change(bot, iid)
        elif action == "pv":
            m = await send_item(bot, ADMIN, it)
            tmp_ids.extend(m)
            await tmp(bot, "Так выглядит пост.", Kb(inline_keyboard=[[Btn(text="✖️ Закрыть", callback_data="cancel")]]))
        elif action == "rm":
            await db.del_item(iid)
            await replan(it["set_id"])
            await open_view(bot, ("set", it["set_id"]))
        elif action in ("up", "dn"):
            sd = await db.get_set(it["set_id"])
            if sd["kind"] == "night":  # в ночи меняются местами только однотипные сообщения одной части
                pend = [i for i in await db.items_of(it["set_id"]) if i["status"] == "pending"]
                ids = [i["id"] for i in pend]
                j = ids.index(iid) + (-1 if action == "up" else 1)
                if not (0 <= j < len(pend)) or (pend[j]["role"], pend[j].get("part")) != (it["role"], it.get("part")):
                    await c.answer("В ночи местами меняются только напоминания одной части", show_alert=True)
                    return
            await db.move(iid, -1 if action == "up" else 1)
            await replan(it["set_id"])
            await render(bot)
        elif action == "in":
            await clear_tmp(bot)
            mode = ("ins", iid, parts[1])
            await tmp(bot, f"Отправь сообщение: оно станет «{ROLE[parts[1]]}» сразу после этого.", cancel_kb)
        elif action == "dl":
            await db.upd_item(iid, delete_at=now())
            await open_view(bot, ("set", it["set_id"]))
        await c.answer()
        return
    await c.answer()


async def start_web() -> None:
    """Отдаёт загруженные фото по публичной ссылке (Telegram берёт по ней предпросмотр)."""
    from aiohttp import web

    async def media(request):
        f = await db.get_file(request.match_info["token"])
        if not f:
            raise web.HTTPNotFound()
        return web.Response(body=f[0], content_type=f[1], headers={"Cache-Control": "public, max-age=86400"})

    async def page(request):
        t = request.match_info["token"]
        base = public_base()
        html_ = (f'<!doctype html><html><head><meta charset="utf-8"><meta property="og:image" content="{base}/m/{t}.jpg">'
                 f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="{base}/m/{t}.jpg">'
                 f'</head><body><img src="{base}/m/{t}.jpg"></body></html>')
        return web.Response(text=html_, content_type="text/html")

    app = web.Application()
    async def root(request):
        return web.Response(text="ok")

    app.router.add_get("/", root)
    app.router.add_get("/m/{token}.jpg", media)
    app.router.add_get("/p/{token}", page)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "8080"))).start()


async def repair_item(bot: Bot) -> None:
    """Разовая правка поста 137: бот записал чужой id сообщения (баг рамки); находим настоящий пост и крепим кнопку."""
    try:
        it = await db.get_item(137)
        ch = await channel()
        if not (it and ch and userbot.enabled() and it["ch_msg_ids"] == "59889" and it["status"] == "sent"):
            return
        head = (it.get("plain") or plain(it["text_html"])).strip()[:30]
        async for m in userbot.client.iter_messages(await userbot._chan(ch), limit=15):
            if m.out and head and head in (m.message or ""):
                await db.upd_item(137, ch_msg_ids=str(m.id))
                logging.warning("REPAIRED item=137 ids=%s", m.id)
                if get_btns(it):
                    await bot.edit_message_reply_markup(chat_id=ch, message_id=m.id, reply_markup=markup(it))
                return
        logging.warning("REPAIR item=137: пост не найден")
    except Exception:
        logging.exception("repair_item failed")


async def main() -> None:
    await db.init(config.DATABASE_URL)
    await start_web()
    await db.acquire_leader()  # ждём, пока завершится предыдущий экземпляр (при деплое), чтобы не было двойных публикаций
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await userbot.start((await bot.get_me()).username)
    for s in await db.active_sets():  # пересчитываем расписание уже запущенных наборов по актуальным правилам
        if s["status"] == "scheduled":
            await replan(s["id"])
    await repair_item(bot)
    asyncio.create_task(worker(bot))
    asyncio.create_task(snap_worker(bot))
    asyncio.create_task(keepalive(bot))
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
