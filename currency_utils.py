"""Валюты, символы и форматирование для мультивалютного режима (5.9)."""
from __future__ import annotations

CURRENCIES: dict[str, dict] = {
    'RUB': {'name': 'Российский рубль',    'symbol': '₽',    'flag': '🇷🇺'},
    'KZT': {'name': 'Казахстанский тенге', 'symbol': '₸',    'flag': '🇰🇿'},
    'BYN': {'name': 'Белорусский рубль',   'symbol': 'Br',   'flag': '🇧🇾'},
    'UZS': {'name': 'Узбекский сум',       'symbol': 'сўм',  'flag': '🇺🇿'},
    'AMD': {'name': 'Армянский драм',       'symbol': '֏',    'flag': '🇦🇲'},
    'GEL': {'name': 'Грузинский лари',     'symbol': '₾',    'flag': '🇬🇪'},
    'AZN': {'name': 'Азербайджанский манат','symbol': '₼',   'flag': '🇦🇿'},
    'KGS': {'name': 'Кыргызский сом',      'symbol': 'с',    'flag': '🇰🇬'},
    'TJS': {'name': 'Таджикский сомони',   'symbol': 'SM',   'flag': '🇹🇯'},
    'TMT': {'name': 'Туркменский манат',   'symbol': 'T',    'flag': '🇹🇲'},
    'USD': {'name': 'Доллар США',          'symbol': '$',    'flag': '🇺🇸'},
    'EUR': {'name': 'Евро',                'symbol': '€',    'flag': '🇪🇺'},
}

DEFAULT_CURRENCY = 'RUB'


def get_currency_symbol(code: str) -> str:
    """Символ валюты по ISO-4217 коду. Неизвестный код → ₽."""
    return CURRENCIES.get(code or DEFAULT_CURRENCY, CURRENCIES[DEFAULT_CURRENCY])['symbol']


def format_amount(amount, code: str = DEFAULT_CURRENCY) -> str:
    """Форматирует сумму с символом валюты: 1 234 ₽ / 1 234 ₸ / …"""
    symbol = get_currency_symbol(code)
    try:
        v = int(float(amount or 0))
        return f"{v:,}".replace(',', '\u00a0') + f"\u00a0{symbol}"
    except Exception:
        return f"0\u00a0{symbol}"
