"""Публикация и удаление в канале от имени аккаунта владельца (чтобы работали премиум-эмодзи).
Содержимое берём из его же сообщений в чате с ботом: так сохраняются все entities, включая custom emoji."""
import copy
import json
import logging
import random

from telethon import TelegramClient
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


async def _send_framed(peer, text: str, ents, url: str) -> int:
    """Текст + картинка как большой предпросмотр ссылки под текстом (скрытая ссылка на картинку в начале поста)."""
    shifted = []
    for e in ents or []:
        e2 = copy.copy(e)
        e2.offset += 1  # впереди добавляем один невидимый символ
        shifted.append(e2)
    shifted.insert(0, types.MessageEntityTextUrl(offset=0, length=1, url=url))
    res = await client(functions.messages.SendMediaRequest(
        peer=peer, media=types.InputMediaWebPage(url=url, force_large_media=True), message="\u200b" + text,
        random_id=random.randrange(-2**63, 2**63), entities=shifted))
    for u in getattr(res, "updates", []) or []:
        if isinstance(u, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            return u.message.id
    raise RuntimeError("не удалось получить id опубликованного сообщения")


async def send(ch, text_ts: int | None, media_ts: int | None, text_plain: str = "",
               media_url: str | None = None, keep_preview: bool = False,
               stored_text: str | None = None, stored_ents: str | None = None) -> list[int]:
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
            return [await _send_framed(peer, text, ents, media_url)]
        except Exception as e:
            logging.warning("framed send failed, fallback to attached media: %s", e)
    if media_ts:
        mm = tm if (tm is not None and media_ts == text_ts and tm.media) else await _find(media_ts, None, True)  # у stored_text медиа ищем по своему времени
        if not mm:
            raise RuntimeError("исходное сообщение в чате с ботом не найдено (удалено или слишком старое?)")
        if len(text) <= 1024:
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


async def delete(ch, ids: list[int]) -> None:
    peer = await _chan(ch)
    await client.delete_messages(peer, ids)


async def stats(ch, msg_id: int) -> dict | None:
    """Просмотры, пересылки, реакции и ответы поста в канале."""
    peer = await _chan(ch)
    m = await client.get_messages(peer, ids=msg_id)
    if not m:
        return None
    reactions = sum(r.count for r in (m.reactions.results if m.reactions and m.reactions.results else []))
    replies = m.replies.replies if m.replies else 0
    return {"views": m.views or 0, "forwards": m.forwards or 0, "reactions": reactions, "replies": replies}
