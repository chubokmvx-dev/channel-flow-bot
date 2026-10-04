"""Публикация и удаление в канале от имени аккаунта владельца (чтобы работали премиум-эмодзи).
Содержимое берём из его же сообщений в чате с ботом: так сохраняются все entities, включая custom emoji."""
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

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


async def send(ch, text_msg: int | None, media_msg: int | None) -> list[int]:
    peer = await _chan(ch)
    ids = [i for i in {text_msg, media_msg} if i]
    msgs = await client.get_messages(_bot_peer, ids=ids)
    by_id = {m.id: m for m in msgs if m}
    tm = by_id.get(text_msg) if text_msg else None
    mm = by_id.get(media_msg) if media_msg else None
    if (text_msg and not tm) or (media_msg and not mm):
        raise RuntimeError("исходное сообщение в чате с ботом не найдено (удалено?)")
    text = (tm.message if tm else "") or ""
    ents = (tm.entities if tm else None) or None
    if mm:
        if len(text) <= 1024:
            m = await client.send_file(peer, mm.media, caption=text or None, formatting_entities=ents)
            return [m.id]
        first = await client.send_file(peer, mm.media)
        second = await client.send_message(peer, text, formatting_entities=ents)
        return [first.id, second.id]
    if not text:
        raise RuntimeError("нечего публиковать")
    m = await client.send_message(peer, text, formatting_entities=ents)
    return [m.id]


async def delete(ch, ids: list[int]) -> None:
    peer = await _chan(ch)
    await client.delete_messages(peer, ids)
