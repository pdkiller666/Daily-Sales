"""
Утилиты для работы с часовыми поясами
"""
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

# Единый дефолтный часовой пояс. Используется как fallback во всём проекте,
# когда у пользователя не задан собственный TZ. Менять здесь — единая точка.
DEFAULT_TZ = 'Europe/Moscow'


def get_user_time(dt, user_timezone=DEFAULT_TZ):
    """
    Конвертирует UTC время в локальное время пользователя
    
    Args:
        dt: datetime объект или строка (может быть naive или aware)
        user_timezone: строка с названием часового пояса (например, 'Europe/Moscow')
    
    Returns:
        datetime объект в часовом поясе пользователя или None при ошибке
    """
    if dt is None:
        return None
        
    try:
        # Если dt это строка, парсим её
        if isinstance(dt, str):
            parsed_dt = None
            # Пробуем разные форматы
            formats = [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",  # ISO формат с микросекундами
                "%Y-%m-%d %H:%M",        # без секунд
            ]
            
            for fmt in formats:
                try:
                    parsed_dt = datetime.strptime(dt, fmt)
                    break
                except ValueError:
                    continue
            
            if parsed_dt is None:
                return None
            
            dt = parsed_dt
        
        # Если dt не datetime объект, возвращаем None
        if not isinstance(dt, datetime):
            return None
        
        # Если datetime naive (без timezone), считаем что это UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo('UTC'))
        
        # Конвертируем в часовой пояс пользователя
        user_tz = ZoneInfo(user_timezone)
        return dt.astimezone(user_tz)
    except Exception:
        return None


def get_utc_time(dt, user_timezone=DEFAULT_TZ):
    """
    Конвертирует локальное время пользователя в UTC
    
    Args:
        dt: datetime объект в локальном времени пользователя
        user_timezone: строка с названием часового пояса
    
    Returns:
        datetime объект в UTC
    """
    try:
        # Если datetime naive, добавляем timezone пользователя
        if dt.tzinfo is None:
            user_tz = ZoneInfo(user_timezone)
            dt = dt.replace(tzinfo=user_tz)
        
        # Конвертируем в UTC
        return dt.astimezone(ZoneInfo('UTC'))
    except Exception:
        return dt


def format_user_datetime(dt, user_timezone=DEFAULT_TZ, format_str='%d.%m.%Y %H:%M'):
    """
    Форматирует datetime в строку с учетом часового пояса пользователя
    
    Args:
        dt: datetime объект или строка
        user_timezone: часовой пояс пользователя (по умолчанию Europe/Moscow)
        format_str: формат вывода даты
    
    Returns:
        Отформатированная строка с датой и временем или "Неизвестно" при ошибке
    """
    if dt is None:
        return "Неизвестно"
    
    # Используем дефолтный timezone если не указан
    if user_timezone is None or user_timezone == '':
        user_timezone = DEFAULT_TZ
        
    try:
        user_dt = get_user_time(dt, user_timezone)
        if user_dt is None:
            # datetime-объект нельзя сконвертировать → показываем как есть
            # непарсируемая строка → "Неизвестно" (не показываем мусор пользователю)
            if isinstance(dt, datetime):
                return str(dt)
            return "Неизвестно"
        return user_dt.strftime(format_str)
    except Exception:
        if isinstance(dt, datetime):
            return str(dt)
        return "Неизвестно"


def get_common_timezones():
    """
    Возвращает список популярных часовых поясов для России и СНГ
    
    Returns:
        dict: Словарь {название для отображения: timezone}
    """
    return {
        '🇷🇺 Москва (МСК, UTC+3)': 'Europe/Moscow',
        '🇷🇺 Калининград (UTC+2)': 'Europe/Kaliningrad',
        '🇷🇺 Самара (UTC+4)': 'Europe/Samara',
        '🇷🇺 Екатеринбург (UTC+5)': 'Asia/Yekaterinburg',
        '🇷🇺 Омск (UTC+6)': 'Asia/Omsk',
        '🇷🇺 Красноярск (UTC+7)': 'Asia/Krasnoyarsk',
        '🇷🇺 Иркутск (UTC+8)': 'Asia/Irkutsk',
        '🇷🇺 Якутск (UTC+9)': 'Asia/Yakutsk',
        '🇷🇺 Владивосток (UTC+10)': 'Asia/Vladivostok',
        '🇷🇺 Магадан (UTC+11)': 'Asia/Magadan',
        '🇷🇺 Камчатка (UTC+12)': 'Asia/Kamchatka',
        '🇺🇦 Киев (UTC+2/+3)': 'Europe/Kiev',
        '🇧🇾 Минск (UTC+3)': 'Europe/Minsk',
        '🇰🇿 Алматы (UTC+6)': 'Asia/Almaty',
        '🇺🇿 Ташкент (UTC+5)': 'Asia/Tashkent',
    }


def validate_timezone(timezone_str):
    """
    Проверяет, является ли строка валидным часовым поясом
    
    Args:
        timezone_str: строка с названием часового пояса
    
    Returns:
        bool: True если валидный, False если нет
    """
    try:
        ZoneInfo(timezone_str)
        return True
    except Exception:
        return False


def get_current_user_time(user_timezone=DEFAULT_TZ):
    """
    Возвращает текущее время в часовом поясе пользователя
    
    Args:
        user_timezone: часовой пояс пользователя
    
    Returns:
        datetime объект в часовом поясе пользователя
    """
    return datetime.now(ZoneInfo(user_timezone))
