"""
Модуль для управления резервными копиями базы данных.
Все бэкап-файлы шифруются AES-256 (Fernet) ключом, выведенным из BOT_TOKEN.
"""
import base64
import hashlib
import logging
import glob
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path


def _derive_fernet_key() -> bytes | None:
    """Выводит 32-байтный Fernet-ключ из BOT_TOKEN через PBKDF2-SHA256.
    Возвращает None если BOT_TOKEN не задан или библиотека недоступна."""
    try:
        token = os.getenv('BOT_TOKEN', '').strip()
        if not token:
            return None
        raw = hashlib.pbkdf2_hmac(
            'sha256',
            token.encode(),
            b'dailysales-backup-salt-v1',
            100_000,
            dklen=32,
        )
        return base64.urlsafe_b64encode(raw)
    except Exception as exc:
        logging.warning('backup: не удалось вывести ключ шифрования: %s', exc)
        return None


def _get_fernet():
    """Возвращает экземпляр Fernet или None если шифрование недоступно."""
    try:
        from cryptography.fernet import Fernet
        key = _derive_fernet_key()
        if key is None:
            return None
        return Fernet(key)
    except ImportError:
        logging.warning('backup: cryptography не установлена — бэкапы не шифруются')
        return None


class BackupManager:
    def __init__(self, db_path='data/main.db', backup_dir='data/backup'):
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.ensure_backup_directory()

    def ensure_backup_directory(self):
        Path(self.backup_dir).mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────
    #  Создание бэкапа
    # ─────────────────────────────────────────────────────────────────

    def create_backup(self, custom_db_path=None, custom_label=None):
        """Создаёт зашифрованную резервную копию БД (.db.enc).
        Если Fernet недоступен — сохраняет обычный .db (с предупреждением)."""
        try:
            target_db = custom_db_path or self.db_path
            if not os.path.exists(target_db):
                logging.error('backup: БД не найдена: %s', target_db)
                return False, 'База данных не найдена'

            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            label = custom_label or 'main'
            fernet = _get_fernet()

            if fernet:
                backup_filename = f'backup_{label}_{timestamp}.db.enc'
                backup_path = os.path.join(self.backup_dir, backup_filename)
                # Сначала SQLite-бэкап во временный файл
                with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    src = sqlite3.connect(target_db)
                    dst = sqlite3.connect(tmp_path)
                    src.backup(dst)
                    src.close()
                    dst.close()
                    # Шифруем и сохраняем
                    with open(tmp_path, 'rb') as f:
                        plaintext = f.read()
                    ciphertext = fernet.encrypt(plaintext)
                    with open(backup_path, 'wb') as f:
                        f.write(ciphertext)
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            else:
                logging.warning('backup: шифрование недоступно, сохраняем открытый .db')
                backup_filename = f'backup_{label}_{timestamp}.db'
                backup_path = os.path.join(self.backup_dir, backup_filename)
                src = sqlite3.connect(target_db)
                dst = sqlite3.connect(backup_path)
                src.backup(dst)
                src.close()
                dst.close()

            logging.info('backup: создан %s', backup_filename)
            return True, f'Резервная копия создана: {backup_filename}'

        except Exception as exc:
            logging.error('backup create: %s', exc)
            return False, f'Ошибка: {exc}'

    def backup_all_tenants(self):
        """Создаёт бэкапы всех БД (main + shop_bot + тенанты)."""
        results = []

        ok, msg = self.create_backup('data/main.db', 'main')
        results.append(f"main.db: {'✅' if ok else '❌'} {msg}")

        if os.path.exists('data/shop_bot.db'):
            ok, msg = self.create_backup('data/shop_bot.db', 'shop_bot')
            results.append(f"shop_bot.db: {'✅' if ok else '❌'} {msg}")

        tenants_dir = 'data/tenants'
        if os.path.exists(tenants_dir):
            for f in sorted(os.listdir(tenants_dir)):
                if f.endswith('.db'):
                    ok, msg = self.create_backup(
                        os.path.join(tenants_dir, f),
                        f.replace('.db', ''),
                    )
                    results.append(f"Tenant {f}: {'✅' if ok else '❌'} {msg}")

        return results

    # ─────────────────────────────────────────────────────────────────
    #  Очистка старых бэкапов
    # ─────────────────────────────────────────────────────────────────

    def cleanup_old_backups(self, keep_days=30):
        try:
            now = datetime.now()
            deleted = 0
            patterns = [
                os.path.join(self.backup_dir, 'backup_*.db.enc'),
                os.path.join(self.backup_dir, 'backup_*.db'),
                os.path.join(self.backup_dir, 'shop_bot_backup_*.db'),
            ]
            for pattern in patterns:
                for path in glob.glob(pattern):
                    age = (now - datetime.fromtimestamp(os.path.getmtime(path))).days
                    if age > keep_days:
                        os.remove(path)
                        deleted += 1
                        logging.info('backup: удалён старый файл %s', os.path.basename(path))
            return True, f'Удалено старых копий: {deleted}'
        except Exception as exc:
            logging.error('backup cleanup: %s', exc)
            return False, f'Ошибка очистки: {exc}'

    # ─────────────────────────────────────────────────────────────────
    #  Список бэкапов
    # ─────────────────────────────────────────────────────────────────

    def get_backup_list(self):
        try:
            patterns = [
                os.path.join(self.backup_dir, 'backup_*.db.enc'),
                os.path.join(self.backup_dir, 'backup_*.db'),
                os.path.join(self.backup_dir, 'shop_bot_backup_*.db'),
            ]
            seen = set()
            files = []
            for pattern in patterns:
                for path in glob.glob(pattern):
                    if path not in seen:
                        seen.add(path)
                        files.append(path)

            backups = []
            for path in files:
                stat = os.stat(path)
                created = datetime.fromtimestamp(stat.st_mtime)
                backups.append({
                    'filename': os.path.basename(path),
                    'path': path,
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created': created,
                    'age_days': (datetime.now() - created).days,
                    'encrypted': path.endswith('.enc'),
                })
            backups.sort(key=lambda x: x['created'], reverse=True)
            return backups
        except Exception as exc:
            logging.error('backup list: %s', exc)
            return []

    # ─────────────────────────────────────────────────────────────────
    #  Определение целевой БД по имени файла
    # ─────────────────────────────────────────────────────────────────

    def _resolve_db_path(self, backup_filename):
        """Определяет целевой путь БД по имени бэкап-файла.
        Поддерживает как .db.enc (зашифрованные), так и .db (старые)."""
        # Убираем .enc суффикс для парсинга
        name = backup_filename
        if name.endswith('.enc'):
            name = name[:-4]

        m = re.match(r'^backup_(.+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.db$', name)
        if not m:
            logging.error('backup: не удалось определить тип БД из имени: %s', backup_filename)
            return self.db_path

        label = m.group(1)
        if label in ('main', 'central'):
            return 'data/main.db'
        if label == 'shop_bot':
            return 'data/shop_bot.db'
        if label.startswith('org_'):
            return os.path.join('data', 'tenants', f'{label}.db')

        logging.error("backup: неизвестный label '%s' в %s", label, backup_filename)
        return self.db_path

    # ─────────────────────────────────────────────────────────────────
    #  Восстановление
    # ─────────────────────────────────────────────────────────────────

    def restore_backup(self, backup_filename):
        """Восстанавливает БД из резервной копии (поддерживает .db.enc и .db)."""
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)
            if not os.path.exists(backup_path):
                return False, 'Резервная копия не найдена'

            target_path = self._resolve_db_path(backup_filename)

            # Предварительный бэкап текущей БД
            target_label = os.path.splitext(os.path.basename(target_path))[0]
            ok, msg = self.create_backup(target_path, f'pre_restore_{target_label}')
            if not ok:
                return False, f'Не удалось создать пред-restore бэкап: {msg}'

            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            # Если зашифрован — расшифруем во временный файл
            if backup_filename.endswith('.enc'):
                fernet = _get_fernet()
                if fernet is None:
                    return False, 'Невозможно расшифровать бэкап: BOT_TOKEN не задан или cryptography не установлена'
                with open(backup_path, 'rb') as f:
                    ciphertext = f.read()
                try:
                    plaintext = fernet.decrypt(ciphertext)
                except Exception as dec_exc:
                    return False, f'Ошибка расшифровки: {dec_exc}'
                with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                    tmp.write(plaintext)
                    tmp_path = tmp.name
                src_path = tmp_path
            else:
                src_path = backup_path
                tmp_path = None

            # Восстановление через SQLite Backup API
            try:
                src = sqlite3.connect(src_path)
                dst = sqlite3.connect(target_path, timeout=30)
                _last_err = None
                for attempt in range(5):
                    try:
                        with dst:
                            src.backup(dst)
                        _last_err = None
                        break
                    except sqlite3.OperationalError as be:
                        _last_err = be
                        time.sleep(0.5 * (attempt + 1))
                if _last_err:
                    raise _last_err
            finally:
                src.close()
                dst.close()
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

            logging.info('backup: восстановлено %s → %s', backup_filename, target_path)
            return True, f'БД восстановлена из {backup_filename} → {target_path}'

        except Exception as exc:
            logging.error('backup restore: %s', exc)
            return False, f'Ошибка восстановления: {exc}'

    # ─────────────────────────────────────────────────────────────────
    #  Информация о конкретном бэкапе
    # ─────────────────────────────────────────────────────────────────

    def get_backup_info(self, backup_filename):
        """Детальная информация о бэкапе. Для .db.enc — расшифровывает во временный файл."""
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)
            if not os.path.exists(backup_path):
                return None

            stat = os.stat(backup_path)
            tmp_path = None

            try:
                if backup_filename.endswith('.enc'):
                    fernet = _get_fernet()
                    if fernet is None:
                        return {
                            'filename': backup_filename,
                            'size_mb': round(stat.st_size / (1024 * 1024), 2),
                            'created': datetime.fromtimestamp(stat.st_mtime),
                            'integrity': False,
                            'encrypted': True,
                            'error': 'BOT_TOKEN не задан — расшифровка невозможна',
                        }
                    with open(backup_path, 'rb') as f:
                        plaintext = fernet.decrypt(f.read())
                    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                        tmp.write(plaintext)
                        tmp_path = tmp.name
                    read_path = tmp_path
                else:
                    read_path = backup_path

                conn = sqlite3.connect(read_path)
                cur = conn.cursor()
                cur.execute('PRAGMA integrity_check')
                integrity = cur.fetchone()[0]
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = cur.fetchall()
                total = 0
                table_info = []
                for (tname,) in tables:
                    if tname != 'sqlite_sequence':
                        cur.execute(f'SELECT COUNT(*) FROM "{tname}"')
                        cnt = cur.fetchone()[0]
                        total += cnt
                        table_info.append({'table': tname, 'records': cnt})
                conn.close()

                return {
                    'filename': backup_filename,
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created': datetime.fromtimestamp(stat.st_mtime),
                    'integrity': integrity == 'ok',
                    'encrypted': backup_filename.endswith('.enc'),
                    'tables_count': len(tables),
                    'total_records': total,
                    'table_info': table_info,
                }
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        except Exception as exc:
            logging.error('backup info: %s', exc)
            return None

    # ─────────────────────────────────────────────────────────────────
    #  Ежедневная процедура
    # ─────────────────────────────────────────────────────────────────

    def daily_backup_routine(self):
        try:
            results = self.backup_all_tenants()
            ok_clean, msg_clean = self.cleanup_old_backups(30)
            all_ok = all('✅' in r for r in results) and ok_clean
            summary = '\n'.join(results) + f'\nОчистка: {msg_clean}'
            return all_ok, summary
        except Exception as exc:
            logging.error('backup daily: %s', exc)
            return False, f'Ошибка: {exc}'
