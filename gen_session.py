"""Одноразовый скрипт: получить строку сессии для TG_SESSION. Запускать на своём компьютере.
    pip install telethon && python gen_session.py
api_id и api_hash берутся на https://my.telegram.org (API development tools).
Строку сессии положи ТОЛЬКО в Railway Variables (TG_SESSION), никому не показывай: она даёт доступ к аккаунту."""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:  # спросит телефон, код и пароль 2FA
    print("\nTG_SESSION =", client.session.save())
