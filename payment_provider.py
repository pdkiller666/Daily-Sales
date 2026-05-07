"""
Фабрика провайдеров платежей.

Провайдеры:
  'sbp'       — ручная верификация (скриншот перевода)
  'yookassa'  — автоматическая оплата через ЮKassa

Yookassa-библиотека импортируется лениво: если пакет не установлен,
функции возвращают None / 'error', не вызывая падение бота.
"""

import logging

logger = logging.getLogger(__name__)

PROVIDER_SBP = 'sbp'
PROVIDER_YOOKASSA = 'yookassa'


# ---------------------------------------------------------------------------
# Фабричные хелперы
# ---------------------------------------------------------------------------

def get_active_provider(db) -> str:
    """Вернуть активный провайдер ('sbp' или 'yookassa')."""
    return db.get_payment_provider()


# ---------------------------------------------------------------------------
# ЮKassa — создание платежа
# ---------------------------------------------------------------------------

def create_yookassa_payment(
    amount: float,
    description: str,
    metadata: dict,
    return_url: str,
    shop_id: str,
    secret_key: str,
) -> dict | None:
    """
    Создать платёж в ЮKassa через API.

    Возвращает dict с ключами:
        payment_id        — идентификатор платежа
        confirmation_url  — URL для перенаправления пользователя
        status            — 'pending' и т. д.
    или None при ошибке.
    """
    if not shop_id or not secret_key:
        logger.error("create_yookassa_payment: shop_id или secret_key не настроены")
        return None

    try:
        import yookassa
        yookassa.Configuration.account_id = shop_id
        yookassa.Configuration.secret_key = secret_key

        from yookassa import Payment
        import uuid

        payment = Payment.create(
            {
                "amount": {
                    "value": f"{amount:.2f}",
                    "currency": "RUB",
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": return_url,
                },
                "description": description,
                "metadata": metadata,
                "capture": True,
            },
            str(uuid.uuid4()),  # idempotence_key
        )

        return {
            "payment_id": payment.id,
            "confirmation_url": payment.confirmation.confirmation_url,
            "status": payment.status,
        }

    except ImportError:
        logger.error("create_yookassa_payment: пакет 'yookassa' не установлен")
        return None
    except Exception as e:
        logger.error(f"create_yookassa_payment: ошибка API — {e}")
        return None


# ---------------------------------------------------------------------------
# ЮKassa — проверка статуса
# ---------------------------------------------------------------------------

def check_yookassa_payment_status(
    payment_id: str,
    shop_id: str,
    secret_key: str,
) -> str:
    """
    Проверить статус платежа в ЮKassa.

    Возвращает одно из:
        'pending'           — ещё обрабатывается
        'waiting_for_capture' — ожидает подтверждения (capture=auto → не используется)
        'succeeded'         — оплата прошла
        'canceled'          — отменён
        'error'             — ошибка API или пакет не установлен
    """
    if not shop_id or not secret_key:
        logger.error("check_yookassa_payment_status: shop_id или secret_key не настроены")
        return "error"

    try:
        import yookassa
        yookassa.Configuration.account_id = shop_id
        yookassa.Configuration.secret_key = secret_key

        from yookassa import Payment

        payment = Payment.find_one(payment_id)
        return payment.status

    except ImportError:
        logger.error("check_yookassa_payment_status: пакет 'yookassa' не установлен")
        return "error"
    except Exception as e:
        logger.error(f"check_yookassa_payment_status: ошибка — {e}")
        return "error"


# ---------------------------------------------------------------------------
# Форматирование провайдера для UI
# ---------------------------------------------------------------------------

def provider_label(provider: str) -> str:
    """Читаемое название провайдера для отображения в интерфейсе."""
    return {
        PROVIDER_SBP: "💳 СБП (ручная проверка)",
        PROVIDER_YOOKASSA: "🏦 ЮKassa (автоматически)",
    }.get(provider, provider)
