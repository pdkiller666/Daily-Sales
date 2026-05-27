"""Хранилище глобального экземпляра Bot.

Вместо `from main import bot` (вызывает повторный импорт __main__ → Router already attached)
используйте:
    from bot_holder import get_bot
    bot = get_bot()
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot

_bot = None
_username: str | None = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def get_bot():
    return _bot


def set_username(username: str) -> None:
    global _username
    _username = username


def get_username() -> str | None:
    return _username
