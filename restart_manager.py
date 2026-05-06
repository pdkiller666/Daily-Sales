"""
Модуль для управления перезапуском бота
"""
import os
import sys
import asyncio
import logging
import signal
import json
from typing import Optional

class RestartManager:
    def __init__(self):
        self.restart_requested = False
        self.restart_reason = None
        self.post_restart_chat_id: Optional[int] = None
        self.restart_data_file = "data/restart_data.json"
        
    def save_restart_data(self, chat_id: int, reason: str):
        """Сохранение данных о перезапуске в файл"""
        try:
            os.makedirs(os.path.dirname(self.restart_data_file), exist_ok=True)
            data = {
                "chat_id": chat_id,
                "reason": reason,
                "timestamp": asyncio.get_event_loop().time()
            }
            with open(self.restart_data_file, 'w') as f:
                json.dump(data, f)
            logging.info(f"Данные перезапуска сохранены для chat_id: {chat_id}")
        except Exception as e:
            logging.error(f"Ошибка сохранения данных перезапуска: {e}")
    
    def load_restart_data(self) -> Optional[dict]:
        """Загрузка данных о перезапуске из файла"""
        try:
            if os.path.exists(self.restart_data_file):
                with open(self.restart_data_file, 'r') as f:
                    data = json.load(f)
                # Удаляем файл после чтения
                os.remove(self.restart_data_file)
                logging.info(f"Загружены данные перезапуска для chat_id: {data.get('chat_id')}")
                return data
        except Exception as e:
            logging.error(f"Ошибка загрузки данных перезапуска: {e}")
        return None

    async def schedule_restart(self, reason: str = "Database restore", delay: int = 3):
        """Планирование перезапуска бота с задержкой"""
        self.restart_requested = True
        self.restart_reason = reason
        
        # Сохраняем данные о перезапуске в файл перед перезапуском
        if self.post_restart_chat_id:
            self.save_restart_data(self.post_restart_chat_id, reason)
        
        logging.info(f"Запланирован перезапуск через {delay} секунд. Причина: {reason}")
        
        # Ждём завершения текущих операций
        await asyncio.sleep(delay)
        
        # Перезапускаем процесс
        await self._perform_restart()
    
    async def _perform_restart(self):
        """Выполнение перезапуска"""
        try:
            logging.info("Выполняется перезапуск бота...")
            
            # Для среды разработки используем простой перезапуск
            if hasattr(sys, '_getframe'):
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                # Для продакшена используем более безопасный способ
                os.system(f"{sys.executable} {' '.join(sys.argv)} &")
                sys.exit(0)
                
        except Exception as e:
            logging.error(f"Ошибка при перезапуске: {e}")
            # Альтернативный способ перезапуска
            try:
                os.system("pkill -f main.py && python main.py &")
                sys.exit(0)
            except Exception as fallback_error:
                logging.error(f"Критическая ошибка перезапуска: {fallback_error}")
    
    def is_restart_requested(self) -> bool:
        """Проверка, запрошен ли перезапуск"""
        return self.restart_requested
    
    def get_restart_reason(self) -> Optional[str]:
        """Получение причины перезапуска"""
        return self.restart_reason
    
    async def graceful_shutdown(self):
        """Graceful завершение работы перед перезапуском"""
        logging.info("Инициировано graceful завершение работы...")
        
        # Здесь можно добавить логику для корректного завершения
        # операций перед перезапуском
        await asyncio.sleep(1)

# Глобальный экземпляр менеджера перезапуска
restart_manager = RestartManager()