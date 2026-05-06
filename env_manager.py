"""
Модуль для управления переменными окружения
"""
import os
from typing import List, Optional


def _parse_ids(value: str) -> List[int]:
    """Парсит строку с ID (одиночный или через запятую)"""
    try:
        if ',' in value:
            return [int(x.strip()) for x in value.split(',') if x.strip()]
        return [int(value.strip())] if value.strip() else []
    except ValueError:
        return []


class EnvManager:
    def __init__(self, env_file_path: str = 'data/.env'):
        self.env_file_path = env_file_path
        self._ensure_data_dir()
        self._ensure_env_file_exists()

    def _ensure_data_dir(self):
        """Создаёт папку data/ если не существует"""
        data_dir = os.path.dirname(self.env_file_path)
        if data_dir and not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)

    def _ensure_env_file_exists(self):
        """Создаёт data/.env файл если не существует"""
        if not os.path.exists(self.env_file_path):
            with open(self.env_file_path, 'w', encoding='utf-8') as f:
                f.write("BOT_TOKEN=\nADMIN_CHAT_ID=\n")

    def read_env_file(self) -> str:
        """Читает содержимое data/.env файла"""
        try:
            with open(self.env_file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def write_env_file(self, content: str):
        """Записывает содержимое в data/.env файл"""
        self._ensure_data_dir()
        with open(self.env_file_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def get_admin_ids(self) -> List[int]:
        """Получает список ID администраторов.
        Приоритет: os.environ → data/.env
        """
        # 1. Сначала проверяем системные переменные окружения (Amvera, Docker и т.д.)
        env_value = os.environ.get('ADMIN_CHAT_ID', '').strip()
        if env_value:
            return _parse_ids(env_value)

        # 2. Затем читаем из data/.env (локальная разработка / изменения через бот)
        content = self.read_env_file()
        for line in content.split('\n'):
            if line.strip().startswith('ADMIN_CHAT_ID='):
                value = line.strip().split('=', 1)[1].strip()
                if value:
                    return _parse_ids(value)

        return []

    def add_admin_id(self, admin_id: int) -> bool:
        """Добавляет новый ID администратора"""
        current_admins = self.get_admin_ids()
        if admin_id in current_admins:
            return False
        current_admins.append(admin_id)
        return self._update_admin_ids(current_admins)

    def remove_admin_id(self, admin_id: int) -> bool:
        """Удаляет ID администратора"""
        current_admins = self.get_admin_ids()
        if admin_id not in current_admins:
            return False
        if self.is_super_admin(admin_id):
            return False
        if len(current_admins) <= 1:
            return False
        current_admins.remove(admin_id)
        return self._update_admin_ids(current_admins)

    def _update_admin_ids(self, admin_ids: List[int]) -> bool:
        """Обновляет список администраторов в data/.env файле"""
        try:
            content = self.read_env_file()
            lines = content.split('\n')
            new_value = ','.join(map(str, admin_ids)) if admin_ids else ''
            new_line = f'ADMIN_CHAT_ID={new_value}'

            admin_line_index = None
            for i, line in enumerate(lines):
                if line.strip().startswith('ADMIN_CHAT_ID='):
                    admin_line_index = i
                    break

            if admin_line_index is not None:
                lines[admin_line_index] = new_line
            else:
                lines.append(new_line)

            self.write_env_file('\n'.join(lines))
            return True
        except Exception:
            return False

    def is_admin(self, chat_id: int) -> bool:
        """Проверяет, является ли пользователь администратором"""
        return chat_id in self.get_admin_ids()

    def get_main_admin_id(self) -> Optional[int]:
        """Получает ID главного администратора (первого в списке)"""
        admin_ids = self.get_admin_ids()
        return admin_ids[0] if admin_ids else None

    def is_super_admin(self, chat_id: int) -> bool:
        """Проверяет, является ли пользователь супер-администратором"""
        return str(chat_id) == "921098636"

    def get_bot_token(self) -> Optional[str]:
        """Получает токен бота. Приоритет: os.environ → data/.env"""
        token = os.environ.get('BOT_TOKEN')
        if token:
            return token
        content = self.read_env_file()
        for line in content.split('\n'):
            if line.strip().startswith('BOT_TOKEN='):
                value = line.strip().split('=', 1)[1].strip()
                return value if value else None
        return None

    def get_env_variable(self, key: str) -> Optional[str]:
        """Получает переменную окружения. Приоритет: os.environ → data/.env"""
        value = os.environ.get(key)
        if value:
            return value
        content = self.read_env_file()
        for line in content.split('\n'):
            if line.strip().startswith(f'{key}='):
                value = line.strip().split('=', 1)[1].strip()
                return value if value else None
        return None


# Общий синглтон — импортировать вместо создания нового экземпляра в каждом модуле
env_manager = EnvManager()
