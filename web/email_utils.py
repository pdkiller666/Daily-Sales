"""Yandex SMTP email utility for DailySales."""
import os
import smtplib
import ssl
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

SMTP_HOST = 'smtp.yandex.ru'
SMTP_PORT = 465

_BASE_STYLE = """
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#f8fafc;margin:0;padding:0}
  .wrap{max-width:480px;margin:40px auto;background:#fff;border-radius:16px;overflow:hidden;
        box-shadow:0 4px 24px rgba(0,0,0,.08)}
  .hdr{background:linear-gradient(135deg,#3b82f6,#6366f1);padding:32px 32px 28px;text-align:center}
  .hdr-logo{display:inline-flex;align-items:center;justify-content:center;width:56px;height:56px;
            background:rgba(255,255,255,.15);border-radius:14px;font-size:24px;font-weight:900;
            color:#fff;margin-bottom:12px}
  .hdr h1{margin:0;font-size:22px;font-weight:800;color:#fff}
  .hdr p{margin:4px 0 0;font-size:12px;color:rgba(255,255,255,.7);text-transform:uppercase;letter-spacing:.1em}
  .body{padding:32px}
  .body p{margin:0 0 16px;font-size:14px;color:#475569;line-height:1.6}
  .btn{display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#3b82f6,#6366f1);
       color:#fff!important;text-decoration:none;border-radius:12px;font-weight:700;font-size:15px;
       margin:8px 0 24px}
  .note{font-size:12px;color:#94a3b8;line-height:1.5;border-top:1px solid #f1f5f9;padding-top:16px;margin-top:8px}
  .url-box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;
            font-family:monospace;font-size:11px;color:#64748b;word-break:break-all;margin:0 0 16px}
</style>
"""


def _yandex_creds() -> tuple[str, str]:
    return os.getenv('YANDEX_EMAIL', ''), os.getenv('YANDEX_SMTP_PASSWORD', '')


def _mask_email(addr: str) -> str:
    """Маскирует email для логов: ivan@mail.ru → i***@mail.ru (PII-минимизация)."""
    try:
        if not addr or '@' not in addr:
            return '***'
        local, domain = addr.split('@', 1)
        head = local[0] if local else ''
        return f"{head}***@{domain}"
    except Exception:
        return '***'


def _send(to_email: str, subject: str, html: str) -> bool:
    from_email, password = _yandex_creds()
    if not from_email or not password:
        logger.error("email_utils: YANDEX_EMAIL or YANDEX_SMTP_PASSWORD not set")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'DailySales <{from_email}>'
        msg['To'] = to_email
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
            srv.login(from_email, password)
            srv.sendmail(from_email, to_email, msg.as_string())
        logger.info("email_utils: sent '%s' → %s", subject, _mask_email(to_email))
        return True
    except Exception as exc:
        logger.error("email_utils: send error → %s: %s", _mask_email(to_email), exc)
        return False


def send_verification_email(to_email: str, verify_url: str) -> bool:
    subject = "Подтвердите email — DailySales"
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>CRM for Sellers</p>
  </div>
  <div class="body">
    <p>Для завершения регистрации подтвердите ваш email-адрес. Ссылка действует <strong>24 часа</strong>.</p>
    <div style="text-align:center">
      <a href="{verify_url}" class="btn">✅ Подтвердить email</a>
    </div>
    <p>Если кнопка не работает, скопируйте ссылку в браузер:</p>
    <div class="url-box">{verify_url}</div>
    <p class="note">Если вы не регистрировались в DailySales — просто проигнорируйте это письмо.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def send_reset_email(to_email: str, reset_url: str) -> bool:
    subject = "Сброс пароля — DailySales"
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>CRM for Sellers</p>
  </div>
  <div class="body">
    <p>Вы запросили сброс пароля. Ссылка действует <strong>60 минут</strong>.</p>
    <div style="text-align:center">
      <a href="{reset_url}" class="btn">🔑 Сбросить пароль</a>
    </div>
    <p>Если кнопка не работает, скопируйте ссылку в браузер:</p>
    <div class="url-box">{reset_url}</div>
    <p class="note">Если вы не запрашивали сброс пароля — просто проигнорируйте это письмо. Ваш пароль не изменится.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def send_link_notification(to_email: str, first_name: str) -> bool:
    """Notify user that their email was linked to a Telegram account."""
    subject = "Email привязан к аккаунту — DailySales"
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>CRM for Sellers</p>
  </div>
  <div class="body">
    <p>Привет, <strong>{first_name}</strong>!</p>
    <p>Ваш email <strong>{to_email}</strong> успешно привязан к аккаунту DailySales.
       Теперь вы можете входить в веб-кабинет по email и паролю.</p>
    <p class="note">Если вы не привязывали этот email — немедленно смените пароль в настройках Telegram-бота.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def send_smart_alert_email(to_email: str, plain_text: str) -> bool:
    """Отправляет AI-алерт о падении выручки на email администратора."""
    subject = "🤖 AI-алерт DailySales — падение выручки"
    safe_text = plain_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>AI-алерт</p>
  </div>
  <div class="body">
    <p style="font-size:15px;font-weight:600;color:#1e293b">{safe_text}</p>
    <div style="text-align:center;margin-top:8px">
      <a href="https://dailysales.ru/ai-insights" class="btn">📊 Открыть AI-инсайты</a>
    </div>
    <p class="note">Это автоматический алерт от DailySales. Настроить уведомления можно в разделе AI-инсайты.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def send_weekly_digest_email(to_email: str, plain_text: str) -> bool:
    """Отправляет еженедельный AI-дайджест на email администратора."""
    subject = "📊 Еженедельный AI-дайджест — DailySales"
    safe_text = plain_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>Дайджест недели</p>
  </div>
  <div class="body">
    <p style="font-size:15px;font-weight:600;color:#1e293b">{safe_text}</p>
    <div style="text-align:center;margin-top:8px">
      <a href="https://dailysales.ru/ai-insights" class="btn">📊 Открыть AI-инсайты</a>
    </div>
    <p class="note">Это автоматический дайджест от DailySales. Настроить уведомления можно в разделе AI-инсайты.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def send_procurement_advisor_email(to_email: str, plain_text: str) -> bool:
    """Отправляет еженедельный AI-советник по закупкам/неликвиду на email администратора."""
    subject = "📦 AI-советник по закупкам — DailySales"
    safe_text = plain_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>Советник по закупкам</p>
  </div>
  <div class="body">
    <p style="font-size:15px;font-weight:600;color:#1e293b">{safe_text}</p>
    <div style="text-align:center;margin-top:8px">
      <a href="https://dailysales.ru/inventory" class="btn">📦 Открыть склад</a>
    </div>
    <p class="note">Это автоматический еженедельный отчёт от DailySales. Настроить уведомления можно в разделе AI-инсайты.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def send_seller_coach_email(to_email: str, seller_name: str, plain_text: str) -> bool:
    """Отправляет персональный коуч-совет продавцу на email."""
    subject = f"⭐ Твои итоги недели — DailySales"
    safe_text = plain_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
    html = f"""{_BASE_STYLE}
<body><div class="wrap">
  <div class="hdr">
    <div class="hdr-logo">DS</div>
    <h1>DailySales</h1><p>Личный коуч</p>
  </div>
  <div class="body">
    <p>Привет, <strong>{seller_name}</strong>!</p>
    <p style="font-size:15px;font-weight:600;color:#1e293b">{safe_text}</p>
    <div style="text-align:center;margin-top:8px">
      <a href="https://dailysales.ru/dashboard" class="btn">📊 Открыть дашборд</a>
    </div>
    <p class="note">Это автоматический еженедельный коуч-отчёт от DailySales.</p>
  </div>
</div></body>"""
    return _send(to_email, subject, html)


def is_configured() -> bool:
    from_email, password = _yandex_creds()
    return bool(from_email and password)
