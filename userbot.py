"""Публикация и удаление в канале от имени аккаунта владельца (чтобы работали премиум-эмодзи).
Содержимое берём из его же сообщений в чате с ботом: так сохраняются все entities, включая custom emoji."""
import asyncio
import copy
import io
import json
import logging
import random
import re

from telethon import TelegramClient
from telethon import utils
from telethon.sessions import StringSession
from telethon.tl import functions, types

import config

client: TelegramClient | None = None
_bot_peer = None


def enabled() -> bool:
    return client is not None


async def start(bot_username: str) -> None:
    global client, _bot_peer
    if not (config.TG_API_ID and config.TG_API_HASH and config.TG_SESSION):
        logging.info("userbot: не настроен, публикуем через Bot API")
        return
    c = TelegramClient(StringSession(config.TG_SESSION), config.TG_API_ID, config.TG_API_HASH)
    await c.connect()
    if not await c.is_user_authorized():
        logging.error("userbot: сессия недействительна, публикуем через Bot API")
        await c.disconnect()
        return
    await c.get_dialogs()  # прогреваем кэш каналов
    _bot_peer = await c.get_entity(bot_username)
    client = c
    me = await c.get_me()
    logging.info("userbot: работает от аккаунта id=%s premium=%s", me.id, getattr(me, "premium", None))


async def _chan(ch):
    try:
        return await client.get_input_entity(ch)
    except ValueError:
        await client.get_dialogs()
        return await client.get_input_entity(ch)


async def _find(ts: int, text: str | None = None, want_media: bool = False):
    """Ищет своё сообщение в чате с ботом по времени отправки (±1 с), при необходимости по тексту и наличию медиа."""
    cands = []
    async for m in client.iter_messages(_bot_peer, limit=400):
        t = int(m.date.timestamp())
        if t < ts - 5:
            break
        if not m.out or abs(t - ts) > 1:
            continue
        if want_media and not m.media:
            continue
        cands.append(m)
    if text is not None:
        exact = [m for m in cands if (m.message or "").strip() == text.strip()]
        if exact:
            return exact[0]
    return cands[0] if cands else None


_ENT = {
    "bold": types.MessageEntityBold, "italic": types.MessageEntityItalic, "underline": types.MessageEntityUnderline,
    "strikethrough": types.MessageEntityStrike, "spoiler": types.MessageEntitySpoiler, "code": types.MessageEntityCode,
    "url": types.MessageEntityUrl, "mention": types.MessageEntityMention, "hashtag": types.MessageEntityHashtag,
    "cashtag": types.MessageEntityCashtag, "bot_command": types.MessageEntityBotCommand, "email": types.MessageEntityEmail,
    "phone_number": types.MessageEntityPhone,
}


def entities_from_json(raw: str | None) -> list:
    """Entities, сохранённые из сообщения бота (aiogram), превращаем в Telethon: так посту не нужно искать исходное сообщение."""
    out = []
    for d in json.loads(raw) if raw else []:
        t, o, n = d["type"], d["offset"], d["length"]
        if t == "text_link":
            out.append(types.MessageEntityTextUrl(offset=o, length=n, url=d.get("url") or ""))
        elif t == "custom_emoji":
            out.append(types.MessageEntityCustomEmoji(offset=o, length=n, document_id=int(d["custom_emoji_id"])))
        elif t == "pre":
            out.append(types.MessageEntityPre(offset=o, length=n, language=d.get("language") or ""))
        elif t in ("blockquote", "expandable_blockquote"):
            out.append(types.MessageEntityBlockquote(offset=o, length=n, collapsed=(t == "expandable_blockquote")))
        elif t in _ENT:
            out.append(_ENT[t](offset=o, length=n))
    return out


WARNINGS: list[str] = []   # что пошло не по плану при публикации: бот прочитает и напишет владельцу


async def _preview_ready(url: str) -> bool:
    """Есть ли у Telegram уже готовый предпросмотр этой ссылки."""
    try:
        r = await client(functions.messages.GetWebPageRequest(url=url, hash=0))
    except Exception as e:
        logging.info("prewarm failed: %s", e)
        return False
    w = getattr(r, "webpage", r)
    return isinstance(w, types.WebPage)


async def prewarm(url: str) -> bool:
    return await _preview_ready(url)


async def ensure_preview(url: str, wait: float = 6.0) -> str | None:
    """Ждёт, пока Telegram скачает и разберёт ссылку; пробует саму картинку, затем страницу с og:image.
    Возвращает ссылку, у которой предпросмотр готов (или None, если не дождались)."""
    page = re.sub(r"/m/([\w]+)\.jpg$", r"/p/\1", url)
    cands = [url] + ([page] if page != url else [])
    cands += [c + "?v=2" for c in cands]   # свежий адрес: Telegram мог закешировать неудачу по старому
    loop = asyncio.get_event_loop()
    end = loop.time() + wait
    delay = 0.4
    while True:
        for u in cands:
            if await _preview_ready(u):
                return u
        if loop.time() >= end:
            return None
        await asyncio.sleep(delay)
        delay = min(delay * 1.6, 2.0)


async def _send_framed(peer, text: str, ents, url: str) -> int:
    """Текст + картинка как большой предпросмотр ссылки под текстом (скрытая ссылка на картинку в начале поста)."""
    shifted = []
    for e in ents or []:
        e2 = copy.copy(e)
        e2.offset += 1  # впереди добавляем один невидимый символ
        shifted.append(e2)
    shifted.insert(0, types.MessageEntityTextUrl(offset=0, length=1, url=url))
    async def go():
        return await client(functions.messages.SendMediaRequest(
            peer=peer, media=types.InputMediaWebPage(url=url, force_large_media=True), message="\u200b" + text,
            random_id=random.randrange(-2**63, 2**63), entities=shifted))
    page = re.sub(r"/m/([\w]+)\.jpg$", r"/p/\1", url)
    base = url.split("?")[0]
    page = re.sub(r"/m/([\w]+)\.jpg$", r"/p/\1", base)
    tries = [url, url, page, base + "?v=2", page + "?v=2"] if page != base else [url, url, base + "?v=2"]
    res, err = None, None
    for i, u in enumerate(tries):
        if u != url:
            url = u
            shifted[0] = types.MessageEntityTextUrl(offset=0, length=1, url=url)
        try:
            res = await go()
            break
        except Exception as e:
            if "WEBPAGE_NOT_FOUND" not in str(e):
                raise
            err = e
            # Telegram качает страницу асинхронно: просим загрузить предпросмотр и ждём, пока он появится
            await prewarm(url)
            await asyncio.sleep(1.5 + i * 0.5)
    if res is None:
        raise err
    want = utils.get_peer_id(peer)
    for u in getattr(res, "updates", []) or []:
        if isinstance(u, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            try:
                same = utils.get_peer_id(u.message.peer_id) == want
            except Exception:
                same = False
            if same:
                return u.message.id
            logging.warning("framed: пропускаю обновление чужого чата id=%s", getattr(u.message, "id", None))
    # id не нашёлся в ответе: берём своё последнее сообщение в канале с таким текстом
    async for m in client.iter_messages(peer, limit=5):
        if m.out and (m.message or "").strip()[:40] == text.strip()[:40]:
            return m.id
    raise RuntimeError("не удалось получить id опубликованного сообщения")


async def _send_bytes(peer, kind: str | None, data: bytes, name: str, text: str, ents) -> list[int]:
    """Публикация медиа из скачанного файла (запасной путь, когда исходное сообщение в чате с ботом не найдено)."""
    def fresh():
        f = io.BytesIO(data)
        f.name = name
        return f
    kw = {}
    if kind == "voice":
        kw["voice_note"] = True
    if kind == "document":
        kw["force_document"] = True
    if len(text) > 1024:
        first = await client.send_message(peer, text, formatting_entities=ents, link_preview=False)
        second = await client.send_file(peer, fresh(), **kw)
        return [first.id, second.id]
    if kind in ("photo", "video", "animation") and text:
        try:
            m = await client.send_file(peer, fresh(), caption=text, formatting_entities=ents, invert_media=True, **kw)
            return [m.id]
        except TypeError:
            pass
    m = await client.send_file(peer, fresh(), caption=text or None, formatting_entities=ents, **kw)
    return [m.id]


async def send(ch, text_ts: int | None, media_ts: int | None, text_plain: str = "",
               media_url: str | None = None, keep_preview: bool = False,
               stored_text: str | None = None, stored_ents: str | None = None,
               media_type: str | None = None, media_fallback=None) -> list[int]:
    peer = await _chan(ch)
    if stored_text is not None:
        tm = None
        text, ents = stored_text, entities_from_json(stored_ents) or None
    else:
        tm = await _find(text_ts, text_plain) if text_ts else None
        if text_ts and not tm:
            raise RuntimeError("исходное сообщение в чате с ботом не найдено (удалено или слишком старое?)")
        text = (tm.message if tm else "") or ""
        ents = (tm.entities if tm else None) or None
    if media_url and text.strip():
        try:
            ready = await ensure_preview(media_url)
            if not ready:
                logging.warning("preview not ready in time for %s, trying anyway", media_url)
            return [await _send_framed(peer, text, ents, ready or media_url)]
        except Exception as e:
            logging.warning("framed send failed, fallback to attached media: %s", e)
            WARNINGS.append(f"карточка с фото не вышла ({str(e)[:120]}), фото ушло вложением")
    if media_ts or (media_type and media_fallback):
        mm = None
        if media_ts:
            mm = tm if (tm is not None and media_ts == text_ts and tm.media) else await _find(media_ts, None, True)  # у stored_text медиа ищем по своему времени
        if not mm:
            if media_fallback:   # исходное сообщение в чате с ботом не нашли: берём файл у бота по file_id и загружаем сами
                logging.warning("SEND via downloaded media: type=%s media_ts=%s", media_type, media_ts)
                data, name = await media_fallback()
                return await _send_bytes(peer, media_type, data, name, text, ents)
            raise RuntimeError("исходное сообщение в чате с ботом не найдено (удалено или слишком старое?)")
        if mm.sticker or isinstance(mm.media, types.MessageMediaDice):   # стикеры и анимированные эмодзи: без подписи
            if isinstance(mm.media, types.MessageMediaDice):
                m = await client.send_file(peer, types.InputMediaDice(emoticon=mm.media.emoticon))
            else:
                m = await client.send_file(peer, mm.media)
            return [m.id]
        if len(text) <= 1024:
            if mm.voice or mm.audio:  # голосовые и аудио: подпись обычная, без «над медиа»
                m = await client.send_file(peer, mm.media, caption=text or None, formatting_entities=ents)
                return [m.id]
            try:
                m = await client.send_file(peer, mm.media, caption=text or None, formatting_entities=ents, invert_media=True)
            except TypeError:  # старая версия Telethon без invert_media
                m = await client.send_file(peer, mm.media, caption=text or None, formatting_entities=ents)
            return [m.id]
        first = await client.send_message(peer, text, formatting_entities=ents, link_preview=False)
        second = await client.send_file(peer, mm.media)
        return [first.id, second.id]
    if not text:
        raise RuntimeError("нечего публиковать")
    m = await client.send_message(peer, text, formatting_entities=ents, link_preview=keep_preview)
    return [m.id]


async def edit(ch, msg_id: int, text: str, stored_ents: str | None, media_url: str | None = None, keep_preview: bool = False) -> None:
    """Правит текст уже опубликованного сообщения (с форматированием, премиум-эмодзи, скрытыми ссылками)."""
    peer = await _chan(ch)
    ents = entities_from_json(stored_ents) or None
    try:
        if media_url and text.strip():   # карточка: скрытая ссылка на картинку в начале поста
            url = await ensure_preview(media_url) or media_url
            shifted = []
            for e in ents or []:
                e2 = copy.copy(e)
                e2.offset += 1
                shifted.append(e2)
            shifted.insert(0, types.MessageEntityTextUrl(offset=0, length=1, url=url))
            await client(functions.messages.EditMessageRequest(
                peer=peer, id=msg_id, message="\u200b" + text, entities=shifted,
                media=types.InputMediaWebPage(url=url, force_large_media=True)))
        else:
            await client.edit_message(peer, msg_id, text, formatting_entities=ents, link_preview=keep_preview)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e).upper() or "not modified" in str(e).lower():
            return
        raise


async def edit_photo(ch, msg_id: int, data: bytes, caption: str | None, stored_ents: str | None) -> None:
    """Заменяет фото в уже опубликованном сообщении; подпись и её форматирование сохраняются."""
    peer = await _chan(ch)
    f = await client.upload_file(data, file_name="photo.jpg")
    ents = entities_from_json(stored_ents) or None
    await client(functions.messages.EditMessageRequest(
        peer=peer, id=msg_id, message=caption or "", entities=ents if caption else None,
        media=types.InputMediaUploadedPhoto(file=f), invert_media=True if caption else None))


REACTIONS = ["👍", "❤", "🔥"]


async def react(ch, msg_id: int) -> str | None:
    """Ставит реакции от аккаунта (у Premium до 3 сразу). Возвращает текст ошибки или None."""
    peer = await _chan(ch)
    last = None
    for attempt in range(3):
        for emojis in (REACTIONS, REACTIONS[:1]):   # если нельзя три сразу, хотя бы одну
            try:
                await client(functions.messages.SendReactionRequest(
                    peer=peer, msg_id=msg_id, reaction=[types.ReactionEmoji(emoticon=e) for e in emojis]))
                return None
            except Exception as e:
                last = str(e)
                if "FLOOD" in last.upper() or "NOT_FOUND" in last.upper() or "MESSAGE_ID_INVALID" in last.upper():
                    break   # сообщение ещё не видно или лимит: ждём и повторяем
        await asyncio.sleep(1.5 * (attempt + 1))
    return last


async def delete(ch, ids: list[int]) -> None:
    peer = await _chan(ch)
    await client.delete_messages(peer, ids)


INV_RE = re.compile(r"t\.me/(?:\+|joinchat/)([\w-]+)")
_hash_cache: dict = {}


async def _info(url: str):
    """(peer, request_needed, chat_id) для ссылки-приглашения, созданной этим аккаунтом; иначе None."""
    m = INV_RE.search(url or "")
    if not m:
        return None
    h = m.group(1)
    if h not in _hash_cache:
        info = False
        try:
            r = await client(functions.messages.CheckChatInviteRequest(h))
            if isinstance(r, types.ChatInviteAlready):
                peer = await client.get_input_entity(r.chat)
                ex = await client(functions.messages.GetExportedChatInviteRequest(peer=peer, link=url))
                inv = getattr(ex, "invite", ex)
                info = (peer, bool(getattr(inv, "request_needed", False)), utils.get_peer_id(r.chat))
        except Exception as e:
            logging.info("track_link: ссылка не отслеживается (%s): %s", h, e)
        _hash_cache[h] = info
    return _hash_cache[h] or None


async def track_link(url: str, title: str, unique: bool = False) -> dict | None:
    """Ссылка-приглашение, созданная этим аккаунтом, отслеживается.
    unique=False: остаётся ваша ссылка как есть, запоминаем её счётчик на момент публикации (вступления считаем разницей);
    unique=True: для сообщения создаётся своя копия ссылки, считаем точно."""
    info = await _info(url)
    if not info:
        return None
    peer, req, chat_id = info
    if not unique:
        inv = await _invite(chat_id, url)
        return {"link": url, "chat": chat_id, "shared": True,
                "base": (getattr(inv, "usage", 0) or 0) + (getattr(inv, "requested", 0) or 0)}
    inv = await client(functions.messages.ExportChatInviteRequest(peer=peer, request_needed=req, title=title[:32]))
    return {"link": inv.link, "chat": chat_id}


async def _invite(chat_id: int, link: str):
    peer = await _chan(chat_id)
    ex = await client(functions.messages.GetExportedChatInviteRequest(peer=peer, link=link))
    return getattr(ex, "invite", ex)


async def joins(track: list[dict]) -> int:
    total = 0
    for t in track:
        inv = await _invite(t["chat"], t["link"])
        cur = (getattr(inv, "usage", 0) or 0) + (getattr(inv, "requested", 0) or 0)
        total += max(0, cur - t["base"]) if t.get("shared") else cur
    return total


async def revoke(track: list[dict]) -> None:
    for t in track:
        if t.get("shared"):
            continue  # общую ссылку пользователя никогда не закрываем
        try:
            peer = await _chan(t["chat"])
            await client(functions.messages.EditExportedChatInviteRequest(peer=peer, link=t["link"], revoked=True))
        except Exception as e:
            logging.info("revoke failed: %s", e)


async def stats(ch, msg_id: int, track: list[dict] | None = None) -> dict | None:
    """Просмотры, пересылки, реакции и ответы поста в канале."""
    peer = await _chan(ch)
    m = await client.get_messages(peer, ids=msg_id)
    if not m:
        return None
    reactions = sum(r.count for r in (m.reactions.results if m.reactions and m.reactions.results else []))
    replies = m.replies.replies if m.replies else 0
    j = 0
    if track:
        try:
            j = await joins(track)
        except Exception as e:
            logging.info("joins failed: %s", e)
    return {"views": m.views or 0, "forwards": m.forwards or 0, "reactions": reactions, "replies": replies, "joins": j}
