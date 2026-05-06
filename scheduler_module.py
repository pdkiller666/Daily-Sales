"""
Модуль для хранения глобального scheduler
"""

# Глобальный scheduler, инициализируется при запуске бота
scheduler = None

def set_scheduler(sched):
    """Устанавливает глобальный scheduler"""
    global scheduler
    scheduler = sched

def get_scheduler():
    """Возвращает глобальный scheduler"""
    return scheduler
