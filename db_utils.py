"""
Утилиты для работы с базами данных в мульти-тенантной системе
"""
import json as _json
import os
import sqlite3
from database import Database
from tenant_manager import tenant_manager
from env_manager import env_manager

# ─── Иконки и метки scope ────────────────────────────────────────────────────

SCOPE_ICONS = {'shop': '🏪', 'city': '🏙️', 'network': '🌐'}
SCOPE_NAMES_RU = {
    'shop':    'Магазин',
    'city':    'Город',
    'network': 'Торговая сеть',
}

# Дефолтные названия должностей (используются если custom_title не задан)
DEFAULT_ROLE_LABELS = {
    'owner':          '👑 Директор',
    'admin_all':      '🛡️ Зам. директора',
    'admin_shop':     '🏪 Упр. магазином',
    'admin_city':     '🏙️ Менеджер города',
    'admin_network':  '🌐 Менеджер сети',
    'user':           '👤 Сотрудник',
}


def get_role_display_label(role: str,
                           scope_type: str = None,
                           scope_values: list = None,
                           custom_title: str = None) -> str:
    """Читаемое название роли/должности.

    Приоритет: custom_title → вычисленный ярлык из роли+scope.
    scope_values — список значений зоны (list[str]).
    """
    if custom_title:
        return custom_title

    if role == 'owner':
        return DEFAULT_ROLE_LABELS['owner']

    if role == 'admin':
        if not scope_type or scope_type == 'all':
            return DEFAULT_ROLE_LABELS['admin_all']

        icon = SCOPE_ICONS.get(scope_type, '📍')
        base_names = {
            'shop':    'Упр. магазином',
            'city':    'Менеджер города',
            'network': 'Менеджер сети',
        }
        label = base_names.get(scope_type, 'Менеджер')

        if scope_values:
            if len(scope_values) == 1:
                label += f' · {scope_values[0]}'
            elif len(scope_values) <= 3:
                label += ' · ' + ', '.join(scope_values)
            else:
                label += f' · {scope_values[0]} +{len(scope_values) - 1}'

        return f'{icon} {label}'

    return DEFAULT_ROLE_LABELS.get('user', '👤 Сотрудник')


def get_user_custom_title(telegram_id: int) -> str | None:
    """Возвращает custom_title пользователя или None."""
    try:
        conn = sqlite3.connect(tenant_manager.main_db_path)
        row = conn.execute(
            "SELECT custom_title FROM user_org_mapping WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ─── Основные функции ─────────────────────────────────────────────────────────

async def get_db(telegram_id, state=None):
    """
    Универсальная функция для получения правильной базы данных для пользователя.
    """
    is_super = env_manager.is_super_admin(telegram_id)

    if state and is_super:
        try:
            data = await state.get_data()
            selected_db = data.get('selected_org_db')
            if selected_db and os.path.exists(selected_db):
                db = Database(selected_db)
                db.create_tables()
                return db
        except Exception:
            pass

    path = tenant_manager.get_user_db_path(telegram_id)

    if path and os.path.exists(path) and path != 'data/shop_bot.db':
        tenant_db = Database(path)
        tenant_db.create_tables()
        return tenant_db

    shop_db = Database('data/shop_bot.db')
    shop_db.create_tables()

    user_in_shop = shop_db.get_user(telegram_id)
    if not user_in_shop:
        central_db = Database('data/main.db')
        user_in_main = central_db.get_user(telegram_id)
        if user_in_main:
            try:
                shop_db.add_user(
                    telegram_id=user_in_main[1],
                    first_name=user_in_main[2],
                    last_name=user_in_main[3],
                    middle_name=user_in_main[4],
                    phone=user_in_main[5],
                    email=user_in_main[6],
                    trade_network=user_in_main[7],
                    shop_name=user_in_main[8],
                    city=user_in_main[9]
                )
            except Exception:
                pass

    return shop_db


async def clear_state_keep_org(state, extra_keys: list | None = None):
    """Очищает state, сохраняя данные выбранной организации суп-админа.

    extra_keys — список дополнительных ключей FSM, которые нужно сохранить
    (например ['excel_start', 'excel_end', 'excel_shop']).
    """
    try:
        data = await state.get_data()
        keep = {
            'selected_org_id':   data.get('selected_org_id'),
            'selected_org_name': data.get('selected_org_name'),
            'selected_org_db':   data.get('selected_org_db'),
            'admin_filter':      data.get('admin_filter'),
        }
        if extra_keys:
            for k in extra_keys:
                v = data.get(k)
                if v is not None:
                    keep[k] = v
        await state.clear()
        if any(v is not None for v in keep.values()):
            await state.update_data(**{k: v for k, v in keep.items() if v is not None})
    except Exception:
        await state.clear()


def is_any_admin(telegram_id: int) -> bool:
    """
    Проверяет является ли пользователь администратором (owner или admin).

    Приоритет:
    0. Глобальный суперадмин — всегда True.
    1. Если в организации — роль из user_org_mapping (owner/admin → True; user → False).
    2. Если НЕ в организации — проверяем env_manager.is_admin() (личный режим).
    """
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        conn = sqlite3.connect(tenant_manager.main_db_path)
        row = conn.execute(
            "SELECT role FROM user_org_mapping WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        if row is not None:
            return row[0] in ('owner', 'admin')
    except Exception:
        pass
    return env_manager.is_admin(telegram_id)


def get_user_org_role(telegram_id: int) -> str | None:
    """Возвращает роль пользователя в организации: 'owner'/'admin'/'user' или None."""
    try:
        conn = sqlite3.connect(tenant_manager.main_db_path)
        row = conn.execute(
            "SELECT role FROM user_org_mapping WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_user_org_scope(telegram_id: int) -> tuple:
    """Возвращает (scope_type, scope_values_list).

    scope_values — list[str]. Пустой список означает полный доступ.
    (None, []) — нет ограничения по зоне (owner, admin_all).

    scope_value в БД хранится как JSON-массив: '["Магазин 1", "Магазин 2"]'
    Старые строковые значения (без '[') обрабатываются как одноэлементный список.
    """
    try:
        conn = sqlite3.connect(tenant_manager.main_db_path)
        row = conn.execute(
            "SELECT role, scope_type, scope_value FROM user_org_mapping WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None, []
        role, scope_type, scope_value = row

        if role == 'owner' or not scope_type or scope_type == 'all':
            return None, []

        values = []
        if scope_value:
            try:
                parsed = _json.loads(scope_value)
                if isinstance(parsed, list):
                    values = [str(v) for v in parsed if v]
                else:
                    values = [str(parsed)]
            except Exception:
                values = [scope_value]

        return scope_type, values
    except Exception:
        return None, []


def get_user_full_scope(telegram_id: int) -> tuple:
    """Возвращает (scope_type, scope_values, custom_title) для пользователя."""
    try:
        conn = sqlite3.connect(tenant_manager.main_db_path)
        row = conn.execute(
            "SELECT role, scope_type, scope_value, custom_title "
            "FROM user_org_mapping WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None, [], None
        role, scope_type, scope_value, custom_title = row

        if role == 'owner' or not scope_type or scope_type == 'all':
            return None, [], custom_title

        values = []
        if scope_value:
            try:
                parsed = _json.loads(scope_value)
                values = [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]
            except Exception:
                values = [scope_value]

        return scope_type, values, custom_title
    except Exception:
        return None, [], None


def is_org_owner(telegram_id: int) -> bool:
    """True если пользователь директор (owner) или глобальный суперадмин."""
    if env_manager.is_super_admin(telegram_id):
        return True
    try:
        conn = sqlite3.connect(tenant_manager.main_db_path)
        row = conn.execute(
            "SELECT role FROM user_org_mapping WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()
        conn.close()
        return row is not None and row[0] == 'owner'
    except Exception:
        return False


def get_db_sync(telegram_id):
    """Синхронная версия get_db для использования вне async контекста."""
    path = tenant_manager.get_user_db_path(telegram_id)
    if path and os.path.exists(path) and path != 'data/shop_bot.db':
        tenant_db = Database(path)
        tenant_db.create_tables()
        return tenant_db

    shop_db = Database('data/shop_bot.db')
    shop_db.create_tables()
    user_in_shop = shop_db.get_user(telegram_id)
    if not user_in_shop:
        central_db = Database('data/main.db')
        user_in_main = central_db.get_user(telegram_id)
        if user_in_main:
            try:
                shop_db.add_user(
                    telegram_id=user_in_main[1],
                    first_name=user_in_main[2],
                    last_name=user_in_main[3],
                    middle_name=user_in_main[4],
                    phone=user_in_main[5],
                    email=user_in_main[6],
                    trade_network=user_in_main[7],
                    shop_name=user_in_main[8],
                    city=user_in_main[9]
                )
            except Exception:
                pass
    return shop_db
