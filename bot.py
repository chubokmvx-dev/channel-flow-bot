import asyncio
import html
import logging
import random
import re
import time
from zoneinfo import ZoneInfo
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    Message,
    MessageOriginChannel,
)

import config
import db

logging.basicConfig(level=logging.INFO)
TZ = ZoneInfo(config.TIMEZONE)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID, F.chat.type == "private")
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)

KIND = {"warmup": "🔥 Розігрів", "post": "📢 Пост", "reminder": "⏰ Нагадування"}
ICON = {"warmup": "🔥", "post": "📢", "reminder": "⏰"}
TTL = {120: "2 хв", 600: "10 хв", 3600: "1 год", 86400: "24 год", 0: "не видаляти"}

# режим очікування наступного повідомлення: ("edit", id) | ("btn", id) | ("ins", id, kind) | ("channel",)
mode: tuple | None = None

# «Текст - https://…» (також приймаємо | та довге тире)
BTN_RE = re.compile(r"^(.+?)\s*(?:\s[-–—]\s|\|)\s*((?:https?|tg)://\S+)$", re.S)


# ---------- налаштування ----------

async def gap() -> int:
    return int(await db.get_setting("gap", "120"))


async def rem_ttl() -> int:
    return int(await db.get_setting("rem_ttl", "120"))


async def start_range() -> tuple[int, int]:
    return int(await db.get_setting("start_min", "120")), int(await db.get_setting("start_max", "300"))


async def channel():
    v = await db.get_setting("channel") or config.CHANNEL_ID
    if not v:
        return None
    return int(v) if v.lstrip("-").isdigit() else v


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S")


def short(item: dict, n: int = 34) -> str:
    return f"{ICON[item['kind']]} {item['preview'][:n] or '(медіа)'}"


# ---------- черга ----------

async def schedule_start() -> None:
    """Викликається, коли в порожню чергу додали перше повідомлення."""
    now = time.time()
    last = float(await db.get_setting("last_sent", "0"))
    g = await gap()
    if last + g > now:
        due = last + g
    else:
        lo, hi = await start_range()
        due = now + random.randint(lo, hi)
    await db.set_setting("next_due", str(due))


async def add_item(msg: Message, kind: str, after_id: int | None = None) -> int:
    if await db.pending() == []:
        await schedule_start()
    preview = msg.text or msg.caption or {
        "photo": "фото", "video": "відео", "animation": "gif", "document": "файл",
        "voice": "голосове", "video_note": "кружок", "audio": "аудіо", "sticker": "стікер",
    }.get(msg.content_type, "медіа")
    ttl = await rem_ttl() if kind == "reminder" else 0
    return await db.add(kind, msg.chat.id, msg.message_id, preview.replace("\n", " "), ttl, after_id)


def etas(items: list[dict], due: float, g: int) -> list[float]:
    now = time.time()
    t = max(due, now)
    out = []
    for _ in items:
        out.append(t)
        t += g
    return out


def kind_kb(current: str) -> list[Btn]:
    return [Btn(text=("✓ " if k == current else "") + v, callback_data=f"k:{k}") for k, v in KIND.items()]


async def next_kind_after(kind: str) -> str:
    if kind == "warmup":
        tail = await db.tail_kinds(2)
        return "post" if tail == ["warmup", "warmup"] else "warmup"
    return "reminder"


async def panel(item_id: int) -> tuple[str, Kb]:
    item = await db.get(item_id)
    nk = await db.get_setting("next_kind", "warmup")
    pend = await db.pending()
    due = float(await db.get_setting("next_due", "0"))
    ids = [p["id"] for p in pend]
    when = ""
    if item_id in ids:
        when = fmt_time(etas(pend, due, await gap())[ids.index(item_id)])
    paused = await db.get_setting("paused", "0") == "1"
    text = (
        f"✅ Додано: {KIND[item['kind']]}\n"
        f"У черзі: {len(pend)}" + (f", вийде о {when}" if when else "") + ("\n⏸ Публікацію призупинено (/resume)" if paused else "")
        + "\n\nЧим буде наступне повідомлення?"
    )
    kb = Kb(inline_keyboard=[
        kind_kb(nk),
        [Btn(text="🔗 Кнопка", callback_data=f"b:{item_id}"), Btn(text="🗑 Таймер", callback_data=f"t:{item_id}"),
         Btn(text="📋 Черга", callback_data="q")],
    ])
    return text, kb


async def queue_view() -> tuple[str, Kb]:
    pend = await db.pending()
    g = await gap()
    due = float(await db.get_setting("next_due", "0"))
    paused = await db.get_setting("paused", "0") == "1"
    ch = await channel()
    lines = [f"📋 <b>Черга</b> · {'⏸ пауза' if paused else '▶️ працює'} · пауза між кроками {g // 60} хв {g % 60} с"]
    if not ch:
        lines.append("⚠️ Канал не вибрано: /channel")
    sent = await db.recent_sent(3)
    if sent:
        lines.append("\nОстанні опубліковані:")
        for s in reversed(sent):
            lines.append(f"• {fmt_time(s['sent_at'].timestamp())} {html.escape(short(s, 30))}"
                         + (" (видалено)" if s["status"] == "deleted" else ""))
    kb = []
    if pend:
        lines.append("\nДалі:")
        for n, (it, t) in enumerate(zip(pend, etas(pend, due, g)), 1):
            extra = " 🔗" if it["btn_url"] else ""
            extra += f" 🗑{it['delete_after'] // 60}хв" if it["delete_after"] else ""
            lines.append(f"{n}. {fmt_time(t)} {html.escape(short(it, 30))}{extra}")
            kb.append([Btn(text=f"{n}. {short(it, 30)}", callback_data=f"i:{it['id']}")])
    else:
        lines.append("\nЧерга порожня. Просто кидай повідомлення, я їх опублікую.")
    kb.append([Btn(text="🔄 Оновити", callback_data="q")])
    return "\n".join(lines), Kb(inline_keyboard=kb[:40])


def item_kb(it: dict) -> Kb:
    i = it["id"]
    return Kb(inline_keyboard=[
        [Btn(text="✏️ Замінити", callback_data=f"e:{i}"), Btn(text="🔗 Кнопка", callback_data=f"b:{i}")],
        [Btn(text="🗑 Таймер видалення", callback_data=f"t:{i}"), Btn(text="❌ З черги", callback_data=f"d:{i}")],
        [Btn(text="⬆️", callback_data=f"u:{i}"), Btn(text="⬇️", callback_data=f"n:{i}")],
        [Btn(text="➕ Вставити після:", callback_data="noop")],
        [Btn(text=v, callback_data=f"ins:{i}:{k}") for k, v in KIND.items()],
        [Btn(text="◀️ До черги", callback_data="q")],
    ])


def item_info(it: dict) -> str:
    b = f"\n🔗 Кнопка: {html.escape(it['btn_text'])} → {html.escape(it['btn_url'])}" if it["btn_url"] else ""
    ttl = f"видалити через {TTL.get(it['delete_after'], str(it['delete_after']) + ' с')}" if it["delete_after"] else "не видаляти"
    return f"<b>{KIND[it['kind']]}</b>\nТаймер: {ttl}{b}"


def markup_for(it: dict) -> Kb | None:
    if it["btn_url"]:
        return Kb(inline_keyboard=[[Btn(text=it["btn_text"], url=it["btn_url"])]])
    return None


async def show_item(chat_id: int, bot: Bot, item_id: int) -> None:
    it = await db.get(item_id)
    if not it or it["status"] != "pending":
        await bot.send_message(chat_id, "Цього елемента вже немає в черзі.")
        return
    try:
        await bot.copy_message(chat_id, it["src_chat"], it["src_msg"], reply_markup=markup_for(it))
    except Exception:
        await bot.send_message(chat_id, "⚠️ Не вдалося показати оригінал (повідомлення видалене?). Заміни його.")
    await bot.send_message(chat_id, item_info(it), reply_markup=item_kb(it))


# ---------- воркер ----------

async def notify(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(config.ADMIN_ID, text)
    except Exception:
        logging.exception("notify failed")


async def tick(bot: Bot) -> None:
    ch = await channel()
    for it in await db.due_deletions():
        try:
            if ch:
                await bot.delete_message(ch, it["ch_msg_id"])
        except Exception as e:
            logging.warning("delete failed: %s", e)
        await db.update(it["id"], status="deleted", delete_at=None)
    if not ch or await db.get_setting("paused", "0") == "1":
        return
    if time.time() < float(await db.get_setting("next_due", "0")):
        return
    pend = await db.pending()
    if not pend:
        return
    it = pend[0]
    now = time.time()
    try:
        sent = await bot.copy_message(ch, it["src_chat"], it["src_msg"], reply_markup=markup_for(it))
        await db.mark_sent(it["id"], sent.message_id, it["delete_after"])
    except Exception as e:
        await db.update(it["id"], status="failed")
        await notify(bot, f"❌ Не вдалося опублікувати {html.escape(short(it))}: {html.escape(str(e))}\n"
                          "Перевір, що бот адмін каналу й повідомлення не видалене.")
    await db.set_setting("last_sent", str(now))
    await db.set_setting("next_due", str(now + await gap()))
    if len(pend) == 1:
        await notify(bot, "✅ Усе з черги опубліковано.")


async def worker(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            logging.exception("tick failed")
        await asyncio.sleep(3)


# ---------- команди ----------

HELP = (
    "<b>Як користуватись</b>\n"
    "Кидай мені повідомлення по черзі: розігрів, розігрів, пост, нагадування, нагадування… "
    "Тип наступного я підказую сам, його можна змінити кнопками під повідомленням.\n"
    "Перше повідомлення вийде в канал через 2–5 хв, далі кожен крок через 2 хв. "
    "Нагадування видаляються з каналу через 2 хв після публікації.\n\n"
    "/queue або /contentplan: черга з часом виходу й редагуванням\n"
    "/pause, /resume: пауза та продовження\n"
    "/channel: вибрати канал (бот має бути адміном)\n"
    "/gap 120: пауза між кроками, секунди\n"
    "/ttl 120: через скільки секунд видаляти нагадування\n"
    "/startdelay 120 300: затримка першого повідомлення, секунди (від і до)\n"
    "/clear: очистити чергу\n\n"
    "Фото, відео, гіфки, форматований текст (жирний, емодзі) підтримуються. Альбоми поки по одному елементу."
)


@router.message(Command("start", "help"))
async def cmd_start(m: Message):
    await m.answer(HELP)


@router.message(Command("queue", "contentplan"))
async def cmd_queue(m: Message):
    text, kb = await queue_view()
    await m.answer(text, reply_markup=kb)


@router.message(Command("pause"))
async def cmd_pause(m: Message):
    await db.set_setting("paused", "1")
    await m.answer("⏸ Призупинено. /resume, щоб продовжити.")


@router.message(Command("resume"))
async def cmd_resume(m: Message):
    await db.set_setting("paused", "0")
    now = time.time()
    if float(await db.get_setting("next_due", "0")) < now + 30:
        await db.set_setting("next_due", str(now + 30))
    await m.answer("▶️ Продовжую, наступне вийде приблизно через 30 с.")


async def _int_setting(m: Message, c: CommandObject, key: str, label: str):
    if not c.args or not c.args.strip().isdigit():
        await m.answer(f"Вкажи число секунд, напр. /{m.text.split()[0][1:]} 120")
        return
    await db.set_setting(key, c.args.strip())
    await m.answer(f"Готово: {label} = {c.args.strip()} с.")


@router.message(Command("gap"))
async def cmd_gap(m: Message, command: CommandObject):
    await _int_setting(m, command, "gap", "пауза між кроками")


@router.message(Command("ttl"))
async def cmd_ttl(m: Message, command: CommandObject):
    await _int_setting(m, command, "rem_ttl", "видалення нагадувань (для нових)")


@router.message(Command("startdelay"))
async def cmd_startdelay(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or int(parts[0]) > int(parts[1]):
        await m.answer("Приклад: /startdelay 120 300")
        return
    await db.set_setting("start_min", parts[0])
    await db.set_setting("start_max", parts[1])
    await m.answer(f"Перше повідомлення виходитиме через {parts[0]}–{parts[1]} с.")


@router.message(Command("clear"))
async def cmd_clear(m: Message):
    await m.answer("Очистити всю чергу?", reply_markup=Kb(inline_keyboard=[[
        Btn(text="Так, очистити", callback_data="clear"), Btn(text="Ні", callback_data="q")]]))


async def set_channel(m: Message, value) -> None:
    global mode
    try:
        chat = await m.bot.get_chat(value)
        me = await m.bot.get_me()
        member = await m.bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await m.answer(f"Канал «{html.escape(chat.title or str(chat.id))}» знайдено, але бот не адмін. Додай його з правом публікувати й видаляти повідомлення.")
            return
    except Exception as e:
        await m.answer(f"Не вдалося отримати канал: {html.escape(str(e))}\nДодай бота адміном і спробуй ще раз.")
        return
    await db.set_setting("channel", str(chat.id))
    mode = None
    await m.answer(f"✅ Канал: {html.escape(chat.title or str(chat.id))}")


@router.message(Command("channel"))
async def cmd_channel(m: Message, command: CommandObject):
    global mode
    if command.args:
        a = command.args.strip()
        await set_channel(m, int(a) if a.lstrip("-").isdigit() else a)
        return
    mode = ("channel",)
    await m.answer("Перешли мені будь-який пост із каналу (або надішли @username / ID). Бот має бути адміном каналу.")


# ---------- прийом повідомлень ----------

@router.message()
async def on_message(m: Message):
    global mode
    cur = mode
    if cur and cur[0] == "channel":
        if isinstance(m.forward_origin, MessageOriginChannel):
            await set_channel(m, m.forward_origin.chat.id)
        elif m.text:
            t = m.text.strip()
            await set_channel(m, int(t) if t.lstrip("-").isdigit() else t)
        return
    if cur and cur[0] == "btn":
        it = await db.get(cur[1])
        mode = None
        t = (m.text or "").strip()
        if t.lower() in ("прибрати", "-", "видалити"):
            await db.update(cur[1], btn_text=None, btn_url=None)
            await m.answer("Кнопку прибрано.")
        elif (mt := BTN_RE.match(t)):
            await db.update(cur[1], btn_text=mt.group(1).strip()[:60], btn_url=mt.group(2))
            await m.answer("🔗 Кнопку додано.")
        else:
            mode = cur
            await m.answer("Формат: Текст кнопки - https://посилання (або «прибрати»).")
            return
        if it:
            await show_item(m.chat.id, m.bot, cur[1])
        return
    if cur and cur[0] == "edit":
        mode = None
        it = await db.get(cur[1])
        if it and it["status"] == "pending":
            preview = (m.text or m.caption or "(медіа)").replace("\n", " ")
            await db.update(cur[1], src_chat=m.chat.id, src_msg=m.message_id, preview=preview)
            await m.answer("✏️ Замінено.")
            await show_item(m.chat.id, m.bot, cur[1])
        else:
            await m.answer("Цього елемента вже немає в черзі.")
        return
    if cur and cur[0] == "ins":
        mode = None
        new_id = await add_item(m, cur[2], after_id=cur[1])
        text, kb = await panel(new_id)
        await m.answer(text.replace("\n\nЧим буде наступне повідомлення?", ""), reply_markup=Kb(inline_keyboard=[kb.inline_keyboard[1]]))
        return

    # звичайне додавання в кінець черги
    kind = await db.get_setting("next_kind", "warmup")
    item_id = await add_item(m, kind)
    await db.set_setting("next_kind", await next_kind_after(kind))
    text, kb = await panel(item_id)
    await m.answer(text, reply_markup=kb, reply_to_message_id=m.message_id)


# ---------- кнопки ----------

@router.callback_query()
async def on_cb(c: CallbackQuery):
    global mode
    data = c.data or ""
    bot, chat_id = c.bot, c.message.chat.id
    if data == "noop":
        await c.answer()
        return
    if data == "q":
        mode = None
        text, kb = await queue_view()
        try:
            await c.message.edit_text(text, reply_markup=kb)
        except Exception:
            await bot.send_message(chat_id, text, reply_markup=kb)
        await c.answer()
        return
    if data == "clear":
        await db.clear_pending()
        await c.message.edit_text("🧹 Чергу очищено.")
        await c.answer()
        return
    kind, _, rest = data.partition(":")
    if kind == "k":
        await db.set_setting("next_kind", rest)
        rows = c.message.reply_markup.inline_keyboard if c.message.reply_markup else []
        if rows:
            rows = [kind_kb(rest)] + [list(r) for r in rows[1:]]
            await c.message.edit_reply_markup(reply_markup=Kb(inline_keyboard=rows))
        await c.answer(f"Далі: {KIND[rest]}")
        return
    parts = rest.split(":")
    item_id = int(parts[0])
    it = await db.get(item_id)
    if not it or it["status"] != "pending":
        await c.answer("Цього елемента вже немає в черзі", show_alert=True)
        return
    if kind == "i":
        await show_item(chat_id, bot, item_id)
    elif kind == "e":
        mode = ("edit", item_id)
        await bot.send_message(chat_id, "Надішли нове повідомлення, воно замінить це.")
    elif kind == "b":
        mode = ("btn", item_id)
        await bot.send_message(chat_id, "Надішли: <code>Текст кнопки - https://посилання</code>\nАбо «прибрати».")
    elif kind == "t":
        await bot.send_message(chat_id, "Через скільки після публікації видалити з каналу?", reply_markup=Kb(inline_keyboard=[
            [Btn(text=v, callback_data=f"ts:{item_id}:{k_}") for k_, v in list(TTL.items())[:3]],
            [Btn(text=v, callback_data=f"ts:{item_id}:{k_}") for k_, v in list(TTL.items())[3:]],
        ]))
    elif kind == "ts":
        await db.update(item_id, delete_after=int(parts[1]))
        await c.message.edit_text(f"🗑 Таймер: {TTL.get(int(parts[1]))}")
    elif kind == "d":
        await db.remove(item_id)
        await c.message.edit_text("❌ Видалено з черги.")
    elif kind in ("u", "n"):
        ok = await db.move(item_id, -1 if kind == "u" else 1)
        await c.answer("Переміщено" if ok else "Далі нікуди")
        return
    elif kind == "ins":
        mode = ("ins", item_id, parts[1])
        await bot.send_message(chat_id, f"Надішли повідомлення, воно стане «{KIND[parts[1]]}» одразу після цього.")
    await c.answer()


async def main() -> None:
    await db.init(config.DATABASE_URL)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(worker(bot))
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
