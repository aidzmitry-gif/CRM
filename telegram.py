"""Telegram-команды модуля Sales. Бот поднимается в части 11 (aiogram)."""
from __future__ import annotations

from core.runtime.contract import TelegramCommand


async def cmd_deals(*args, **kwargs) -> str:
    """/deals — список активных сделок (заглушка)."""
    return "Список сделок появится в части 11 (Telegram как primary interface)."


COMMANDS = [
    TelegramCommand("deals", "Активные сделки", cmd_deals),
]
