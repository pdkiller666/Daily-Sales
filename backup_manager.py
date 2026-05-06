"""
Модуль для управления резервными копиями базы данных
"""
import os
import re
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
import glob
import logging

class BackupManager:
    def __init__(self, db_path='data/main.db', backup_dir='data/backup'):
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.ensure_backup_directory()

    def ensure_backup_directory(self):
        """Создает директорию для резервных копий если не существует"""
        Path(self.backup_dir).mkdir(parents=True, exist_ok=True)

    def create_backup(self, custom_db_path=None, custom_label=None):
        """Создает резервную копию базы данных (основной или тенанта)"""
        try:
            target_db = custom_db_path or self.db_path
            if not os.path.exists(target_db):
                logging.error(f"База данных не найдена: {target_db}")
                return False, "База данных не найдена"

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            label = custom_label or "main"
            backup_filename = f"backup_{label}_{timestamp}.db"
            backup_path = os.path.join(self.backup_dir, backup_filename)

            source_conn = sqlite3.connect(target_db)
            backup_conn = sqlite3.connect(backup_path)
            source_conn.backup(backup_conn)
            source_conn.close()
            backup_conn.close()

            logging.info(f"Создана резервная копия: {backup_filename}")
            return True, f"Резервная копия создана: {backup_filename}"

        except Exception as e:
            logging.error(f"Ошибка создания резервной копии: {e}")
            return False, f"Ошибка: {str(e)}"

    def backup_all_tenants(self):
        """Создает резервные копии для всех баз данных (main + shop_bot + тенанты)"""
        results = []

        # 1. Бэкап main.db (организации + маппинг)
        success, msg = self.create_backup('data/main.db', "main")
        results.append(f"main.db: {'✅' if success else '❌'} {msg}")

        # 2. Бэкап shop_bot.db (подписки, платежи, личные пользователи)
        if os.path.exists('data/shop_bot.db'):
            success, msg = self.create_backup('data/shop_bot.db', "shop_bot")
            results.append(f"shop_bot.db: {'✅' if success else '❌'} {msg}")

        # 3. Бэкап тенантов
        tenants_dir = 'data/tenants'
        if os.path.exists(tenants_dir):
            for f in sorted(os.listdir(tenants_dir)):
                if f.endswith('.db'):
                    tenant_path = os.path.join(tenants_dir, f)
                    tenant_label = f.replace('.db', '')
                    success, msg = self.create_backup(tenant_path, tenant_label)
                    results.append(f"Tenant {f}: {'✅' if success else '❌'} {msg}")

        return results

    def cleanup_old_backups(self, keep_days=30):
        """Удаляет резервные копии старше указанного количества дней"""
        try:
            current_time = datetime.now()
            deleted_count = 0

            backup_pattern = os.path.join(self.backup_dir, "backup_*.db")
            backup_files = glob.glob(backup_pattern)
            # Совместимость со старым форматом имён
            backup_files.extend(glob.glob(os.path.join(self.backup_dir, "shop_bot_backup_*.db")))

            for backup_file in backup_files:
                # getmtime — время последней модификации (надёжнее getctime на Linux)
                file_time = datetime.fromtimestamp(os.path.getmtime(backup_file))
                age_days = (current_time - file_time).days

                if age_days > keep_days:
                    os.remove(backup_file)
                    deleted_count += 1
                    logging.info(f"Удалена старая резервная копия: {os.path.basename(backup_file)}")

            return True, f"Удалено старых копий: {deleted_count}"

        except Exception as e:
            logging.error(f"Ошибка очистки старых копий: {e}")
            return False, f"Ошибка очистки: {str(e)}"

    def get_backup_list(self):
        """Получает список всех резервных копий"""
        try:
            backup_pattern = os.path.join(self.backup_dir, "backup_*.db")
            backup_files = glob.glob(backup_pattern)
            # Совместимость со старым форматом имён
            backup_files.extend(glob.glob(os.path.join(self.backup_dir, "shop_bot_backup_*.db")))

            backups = []
            for backup_file in backup_files:
                stat = os.stat(backup_file)
                size_mb = round(stat.st_size / (1024 * 1024), 2)
                # getmtime — надёжнее getctime на Linux
                created = datetime.fromtimestamp(stat.st_mtime)

                backups.append({
                    'filename': os.path.basename(backup_file),
                    'path': backup_file,
                    'size_mb': size_mb,
                    'created': created,
                    'age_days': (datetime.now() - created).days
                })

            backups.sort(key=lambda x: x['created'], reverse=True)
            return backups

        except Exception as e:
            logging.error(f"Ошибка получения списка копий: {e}")
            return []

    def _resolve_db_path(self, backup_filename):
        """Определяет целевой путь БД по имени бэкап-файла.

        Формат имени: backup_{label}_{YYYY-MM-DD_HH-MM-SS}.db
        Маппинг:
          label == 'main'      → data/main.db
          label == 'shop_bot'  → data/shop_bot.db
          label == 'central'   → data/main.db  (старый формат)
          label начинается с 'org_' → data/tenants/{label}.db
          иначе               → self.db_path (fallback, лог предупреждения)
        """
        m = re.match(r'^backup_(.+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.db$', backup_filename)
        if not m:
            # Старый формат или неизвестный — fallback
            logging.error(f"Не удалось определить тип БД из имени: {backup_filename}, восстанавливаем в {self.db_path}")
            return self.db_path

        label = m.group(1)

        if label == 'main' or label == 'central':
            return 'data/main.db'
        if label == 'shop_bot':
            return 'data/shop_bot.db'
        if label.startswith('org_'):
            return os.path.join('data', 'tenants', f"{label}.db")

        logging.error(f"Неизвестный label '{label}' в имени {backup_filename}, восстанавливаем в {self.db_path}")
        return self.db_path

    def restore_backup(self, backup_filename):
        """Восстанавливает базу данных из резервной копии в правильный файл"""
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)

            if not os.path.exists(backup_path):
                return False, "Резервная копия не найдена"

            target_path = self._resolve_db_path(backup_filename)

            # Создаём резервную копию текущей базы перед восстановлением
            target_label = os.path.splitext(os.path.basename(target_path))[0]
            current_backup_result = self.create_backup(target_path, f"pre_restore_{target_label}")
            if not current_backup_result[0]:
                return False, f"Не удалось создать резервную копию текущей БД: {current_backup_result[1]}"

            # Убеждаемся, что директория назначения существует (для тенантов)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            shutil.copy2(backup_path, target_path)

            logging.info(f"БД восстановлена из {backup_filename} → {target_path}")
            return True, f"БД восстановлена из {backup_filename} → {target_path}"

        except Exception as e:
            logging.error(f"Ошибка восстановления: {e}")
            return False, f"Ошибка восстановления: {str(e)}"

    def get_backup_info(self, backup_filename):
        """Получает детальную информацию о резервной копии"""
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)

            if not os.path.exists(backup_path):
                return None

            stat = os.stat(backup_path)

            try:
                conn = sqlite3.connect(backup_path)
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                integrity = cursor.fetchone()[0]

                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = cursor.fetchall()

                total_records = 0
                table_info = []
                for table in tables:
                    if table[0] != 'sqlite_sequence':
                        cursor.execute(f"SELECT COUNT(*) FROM {table[0]}")
                        count = cursor.fetchone()[0]
                        total_records += count
                        table_info.append({'table': table[0], 'records': count})

                conn.close()

                return {
                    'filename': backup_filename,
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created': datetime.fromtimestamp(stat.st_mtime),
                    'integrity': integrity == 'ok',
                    'tables_count': len(tables),
                    'total_records': total_records,
                    'table_info': table_info
                }

            except Exception as e:
                return {
                    'filename': backup_filename,
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created': datetime.fromtimestamp(stat.st_mtime),
                    'integrity': False,
                    'error': str(e)
                }

        except Exception as e:
            logging.error(f"Ошибка получения информации о копии: {e}")
            return None

    def daily_backup_routine(self):
        """Ежедневная процедура резервного копирования (все БД + очистка старых)"""
        try:
            results = self.backup_all_tenants()
            cleanup_success, cleanup_msg = self.cleanup_old_backups(30)

            all_ok = all('✅' in r for r in results) and cleanup_success
            summary = "\n".join(results) + f"\nОчистка: {cleanup_msg}"
            return all_ok, summary

        except Exception as e:
            logging.error(f"Ошибка ежедневного копирования: {e}")
            return False, f"Ошибка: {str(e)}"
