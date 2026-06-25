CURRENT_VERSION = "1.196.0"

ENTRIES = [
    {
        "version": "1.196.0",
        "date": "25 июня 2026",
        "items": [
            "Add ability for users to upload payment screenshots via the web interface",
        ],
    },
    {
        "version": "1.195.0",
        "date": "25 июня 2026",
        "items": [
            "Загрузка скриншота оплаты прямо из веб-кабинета: после отправки заявки на модуль или тариф появляется форма прикрепления файла (JPG/PNG/PDF до 10 МБ). Администратор получает уведомление со ссылкой на скриншот.",
            "Improve subscription tab navigation for mobile devices",
        ],
    },
    {
        "version": "1.194.0",
        "date": "25 июня 2026",
        "items": [
            "Make free modules available for purchase and display them as active",
        ],
    },
    {
        "version": "1.193.0",
        "date": "25 июня 2026",
        "items": [
            "Make free modules available for purchase and display them as active",
        ],
    },
    {
        "version": "1.192.0",
        "date": "25 июня 2026",
        "items": [
            "Update deployment status and fix withdrawn request processing time",
        ],
    },
    {
        "version": "1.191.0",
        "date": "25 июня 2026",
        "items": [
            "Add option to purchase additional sales capacity and display unlimited subscription status",
        ],
    },
    {
        "version": "1.190.0",
        "date": "25 июня 2026",
        "items": [
            "Establish a rule for immediate deployment after task merges",
            "Show withdrawn requests differently in История tab",
            "Let admins see withdrawn payment requests in bot payment panel",
            "Show withdrawn request count badge in bot payment panel header",
            "Git commit prior to merge",
            "Add date-range filter to cancelled payments list in bot",
            "Show withdrawn requests to admins in the bot payment panel with a clear label",
        ],
    },
    {
        "version": "1.189.0",
        "date": "25 июня 2026",
        "items": [
            "subscription page UX improvements 8–14 (Task #37)",
            "Fix 10-second freeze when cancelling a module subscription",
            "Let owners withdraw a pending subscription request from the History tab",
        ],
    },
    {
        "version": "1.188.0",
        "date": "25 июня 2026",
        "items": [
            "Страница «Подписка»: полировка — Alpine-попап подтверждения при понижении тарифа (без браузерного confirm()), новая вкладка «📋 История заявок» с пустым состоянием и датой обработки, оптимизация DB: все запросы к shop_bot.db выполняются за одно соединение (_load_shop_bot_data), корректное закрытие соединения downgrade-проверки (try/finally), таймаут уведомления администратора снижен с 10 до 4 с.",
            "subscription page UX improvements 1–7 (Task #36)",
        ],
    },
    {
        "version": "1.187.0",
        "date": "25 июня 2026",
        "items": [
            "Страница «Подписка»: 7 UX-улучшений — мгновенное переключение вкладок без перезагрузки (Alpine.js), переключатель оплаты Мес/Год с автоматическим пересчётом цен, блокировка кнопок при ожидающей заявке, цветовые метки истекающих модулей (⚠ amber/red), возврат на правильную вкладку после POST, онбординг «3 шага» для новых пользователей, раздел «Дополнительный объём» (+100 товаров / +1 магазин) напрямую из веб-кабинета.",
            "Update competitive analysis with new pricing, AI features, and tech metrics",
        ],
    },
    {
        "version": "1.186.0",
        "date": "25 июня 2026",
        "items": [
            "Update documentation with new AI features and APScheduler job count",
        ],
    },
    {
        "version": "1.185.0",
        "date": "25 июня 2026",
        "items": [
            "Update application with new features and improve reporting accuracy",
        ],
    },
    {
        "version": "1.184.0",
        "date": "25 июня 2026",
        "items": [
            "Update application with new features and improve reporting accuracy",
        ],
    },
    {
        "version": "1.183.0",
        "date": "25 июня 2026",
        "items": [
            "Мини-спарклайн (6 недель) в таблице по точкам дайджеста — тренд выручки виден сразу, без перехода на детали",
            "Update application with new features and improve reporting accuracy",
            "Add \"данные на [date]\" freshness label to digest metrics tables",
            "Add plan details to improve AI understanding of store performance",
            "Cache per-digest metrics to eliminate redundant DB reads on /ai-insights page",
            "Git commit prior to merge",
            "Show per-shop weekly revenue trend in the AI insights web panel",
            "Let owners choose which shops appear in the weekly AI digest",
            "Backfill shop_breakdown_json cache when computed on-demand",
            "Add APScheduler job to backfill shop_breakdown_json for all orgs",
            "Add a visual trend indicator to the shop revenue breakdown",
            "Show which shops are included when viewing a past digest",
            "Cache sparkline values back to DB after first enrichment in weekly digest",
            "Re-run breakdown backfill automatically when a new digest is generated",
            "Let owners configure the shop filter from the Telegram bot",
            "Add tooltip showing exact weekly revenue when hovering sparkline bars",
            "log summary after each backfill_shop_breakdown run",
            "Fix stuck 'deleted' red badge appearing on tasks and other pages",
            "Show shop name checkboxes in AI digest settings (not just a count)",
        ],
    },
    {
        "version": "1.182.0",
        "date": "25 июня 2026",
        "items": [
            "Improve plan reporting to accurately differentiate sales categories",
            "Sort per-shop AI insights table by revenue descending",
            "Include intra-org shop breakdown in weekly AI digest",
            "Show per-shop metrics table inside weekly AI digest cards",
        ],
    },
    {
        "version": "1.181.0",
        "date": "25 июня 2026",
        "items": [
            "Update group chat interface to match direct message layout",
            "AI-инсайты — сравнение магазинов внутри одной орг (intra-org mode)",
            "Git commit prior to merge",
            "Add per-shop breakdown table to AI insights page (Task #3)",
        ],
    },
    {
        "version": "1.180.0",
        "date": "25 июня 2026",
        "items": [
            "Update chat interface for easier messaging and conversation management",
        ],
    },
    {
        "version": "1.179.0",
        "date": "25 июня 2026",
        "items": [
            "Fix error when clearing conversations and sending messages",
            "Improve the input field for direct messages",
        ],
    },
    {
        "version": "1.178.0",
        "date": "25 июня 2026",
        "items": [
            "Add functionality to clear chat conversations and topics",
            "Fix issue with old AI messages persisting and improve message sending",
        ],
    },
    {
        "version": "1.177.0",
        "date": "25 июня 2026",
        "items": [
            "Fix error preventing users from accessing the price tag editor",
        ],
    },
    {
        "version": "1.176.0",
        "date": "24 июня 2026",
        "items": [
            "Improve context menu display and update navigation icons",
        ],
    },
    {
        "version": "1.175.0",
        "date": "24 июня 2026",
        "items": [
            "Update navigation icons to prevent duplicates and fix messaging bugs",
        ],
    },
    {
        "version": "1.174.0",
        "date": "24 июня 2026",
        "items": [
            "Fix audio message rendering and standardize time display for messages",
        ],
    },
    {
        "version": "1.173.0",
        "date": "24 июня 2026",
        "items": [
            "Add a message context menu and improve deployment stability",
        ],
    },
    {
        "version": "1.172.0",
        "date": "24 июня 2026",
        "items": [
            "- Чат: единое меню сообщения в стиле Telegram — кликните по сообщению, и появится всплывающее меню с реакциями и действиями (Ответить, Копировать, Переслать, Редактировать, Закрепить, Удалить) вместо мелких кнопок при наведении.",
            "- Чат: новое действие «Копировать» — текст сообщения копируется в буфер обмена одним нажатием.",
            "Address security vulnerabilities and improve performance in chat and subscription features",
            "Securely access direct message files and improve chat functionality",
            "Add screenshot of Telegram message context menu",
        ],
    },
    {
        "version": "1.171.0",
        "date": "24 июня 2026",
        "items": [
            "Add a visual indicator for unread messages in chat conversations",
        ],
    },
    {
        "version": "1.170.0",
        "date": "24 июня 2026",
        "items": [
            "📅 В чате появились разделители по датам — «Сегодня», «Вчера» и точная дата между днями",
            "💬 Сообщения подряд от одного человека теперь сгруппированы: один аватар и имя на серию — как в Telegram",
            "🔵 Линия «непрочитанные сообщения» показывает, на чём вы остановились при открытии диалога или темы",
        ],
    },
    {
        "version": "1.169.0",
        "date": "24 июня 2026",
        "items": [
            "🎙️ Голосовые сообщения в чате: запись с микрофона и удобный плеер с перемоткой и длительностью — в группах и личных сообщениях",
        ],
    },
    {
        "version": "1.168.0",
        "date": "24 июня 2026",
        "items": [
            "Add link preview functionality to chat messages",
            "Add voice message recording and playback functionality to chat",
        ],
    },
    {
        "version": "1.167.0",
        "date": "24 июня 2026",
        "items": [
            "Add ability to view multiple files in direct messages",
        ],
    },
    {
        "version": "1.166.0",
        "date": "24 июня 2026",
        "items": [
            "Add markdown formatting to chat messages for improved readability",
        ],
    },
    {
        "version": "1.165.0",
        "date": "24 июня 2026",
        "items": [
            "Add message forwarding functionality to the chat interface",
        ],
    },
    {
        "version": "1.164.0",
        "date": "23 июня 2026",
        "items": [
            "Add ability to pin messages and improve chat interface",
        ],
    },
    {
        "version": "1.163.0",
        "date": "23 июня 2026",
        "items": [
            "Add real-time chat message delivery using WebSockets",
        ],
    },
    {
        "version": "1.162.0",
        "date": "23 июня 2026",
        "items": [
            "Add message editing, reactions, and replies to chat functionality",
        ],
    },
    {
        "version": "1.161.0",
        "date": "23 июня 2026",
        "items": [
            "Ensure AI chat history is correctly cleared after starting a new dialogue",
        ],
    },
    {
        "version": "1.160.0",
        "date": "23 июня 2026",
        "items": [
            "Update navigation patterns for improved user experience",
        ],
    },
    {
        "version": "1.159.0",
        "date": "23 июня 2026",
        "items": [
            "Update application with new UI components and code structure improvements",
        ],
    },
    {
        "version": "1.158.0",
        "date": "23 июня 2026",
        "items": [
            "Add options to open web interface in Telegram, Android app, or browser",
            "Add screenshots illustrating push notification issues",
        ],
    },
    {
        "version": "1.157.0",
        "date": "23 июня 2026",
        "items": [
            "Add ability to open web app within Telegram and in browser",
        ],
    },
    {
        "version": "1.156.0",
        "date": "23 июня 2026",
        "items": [
            "Update roadmap and preferences to reflect completed tasks",
        ],
    },
    {
        "version": "1.155.0",
        "date": "23 июня 2026",
        "items": [
            "Improve scheduled notification processing by updating user timezones",
        ],
    },
    {
        "version": "1.154.0",
        "date": "23 июня 2026",
        "items": [
            "Improve how scheduled notifications are processed for efficiency",
        ],
    },
    {
        "version": "1.153.0",
        "date": "23 июня 2026",
        "items": [
            "Improve mobile display of buttons across various pages",
        ],
    },
    {
        "version": "1.152.0",
        "date": "23 июня 2026",
        "items": [
            "Adjust task pages to ensure buttons wrap correctly on smaller screens",
        ],
    },
    {
        "version": "1.151.0",
        "date": "23 июня 2026",
        "items": [
            "Update roadmap and application version with new features",
        ],
    },
    {
        "version": "1.150.0",
        "date": "23 июня 2026",
        "items": [
            "Implement database versioning and referral bonus functionality",
            "Update improvement roadmap with completed and remaining tasks",
        ],
    },
    {
        "version": "1.149.0",
        "date": "23 июня 2026",
        "items": [
            "Add two-factor authentication and improve security measures",
        ],
    },
    {
        "version": "1.148.0",
        "date": "23 июня 2026",
        "items": [
            "- 🛡️ Двухфакторная аутентификация (2FA) для входа по email и паролю: код из приложения-аутентификатора + коды восстановления",
            "- 🛡️ Усилена защита форм (CSRF) — уникальный токен на каждый запрос",
            "- 🛡️ Аудит-лог действий супер-админа: биллинг, удаление организаций, сброс подписок, бэкапы",
            "- 🚀 Библиотеки и шрифт теперь грузятся локально — быстрее открытие и единый шрифт во всём кабинете",
            "- ⚙️ Чат экономит трафик: фоновые проверки реже, когда вкладка скрыта",
            "Implement annual billing options and improve payment processing safety",
            "Update roadmap with completed improvements and dates",
        ],
    },
    {
        "version": "1.147.0",
        "date": "23 июня 2026",
        "items": [
            "Update deployment script to correctly push code changes to production",
            "Add a comprehensive improvement roadmap to the project documentation",
            "Add annual subscription options and improve date handling",
        ],
    },
    {
        "version": "1.146.0",
        "date": "22 июня 2026",
        "items": [
            "Update salary calculation logic to exclude paid absences",
            "Improve deployment script to ensure code updates reliably",
        ],
    },
    {
        "version": "1.145.0",
        "date": "22 июня 2026",
        "items": [
            "Correctly calculate salaries by excluding paid absences from shift counts",
        ],
    },
    {
        "version": "1.144.0",
        "date": "22 июня 2026",
        "items": [
            "Improve motivation sync with network alias support and detailed warnings",
        ],
    },
    {
        "version": "1.143.0",
        "date": "22 июня 2026",
        "items": [
            "Improve motivation sync by allowing network aliases and reporting unmatched networks",
        ],
    },
    {
        "version": "1.142.0",
        "date": "22 июня 2026",
        "items": [
            "Improve Google Sheets motivation sync by fixing product matching and reporting",
        ],
    },
    {
        "version": "1.141.0",
        "date": "22 июня 2026",
        "items": [
            "- Синхронизация мотивации через Google Sheets: теперь товары сопоставляются без учёта регистра и лишних пробелов, а в отчёте видно сколько правил реально записано в базу и какие модели не найдены (раньше при несовпадении названий показывался «успех», но мотивация не записывалась).",
            "Remove unnecessary asynchronous operation wrappers for database calls",
        ],
    },
    {
        "version": "1.140.0",
        "date": "22 июня 2026",
        "items": [
            "Remove redundant asynchronous operation wrappers for database calls",
        ],
    },
    {
        "version": "1.139.0",
        "date": "22 июня 2026",
        "items": [
            "Исправлена ошибка кнопки «🎯 Таргетированные правила» — список правил, пагинация, изменение ставки и удаление теперь работают без ошибок.",
            "Fix AI search to display products correctly when filters are active",
            "исправлена двойная обёртка asyncio.to_thread в секции «Таргетированные правила»",
        ],
    },
    {
        "version": "1.138.0",
        "date": "22 июня 2026",
        "items": [
            "Fix issues with displaying product size and category options",
        ],
    },
    {
        "version": "1.137.0",
        "date": "22 июня 2026",
        "items": [
            "Improve navigation and cart stock synchronization",
        ],
    },
    {
        "version": "1.136.0",
        "date": "22 июня 2026",
        "items": [
            "Improve navigation by restoring module state and filters",
        ],
    },
    {
        "version": "1.135.0",
        "date": "22 июня 2026",
        "items": [
            "Add ability to save and restore shopping cart data",
        ],
    },
    {
        "version": "1.134.0",
        "date": "22 июня 2026",
        "items": [
            "Fix AI chat not refreshing after reset by updating polling logic",
            "Add comprehensive documentation for APK delivery system",
        ],
    },
    {
        "version": "1.133.0",
        "date": "21 июня 2026",
        "items": [
            "Add profile photo upload and display in settings and chat",
        ],
    },
    {
        "version": "1.132.0",
        "date": "21 июня 2026",
        "items": [
            "- **Фото профиля сотрудника** — в настройках можно загрузить своё фото (JPG/PNG/WebP до 5 МБ); отображается аватаром в списке контактов и окне переписки в чате",
            "- **Исправлен баг с кнопкой ✕** в ЛС — кнопка удаления сообщения больше не попадает внутрь баббла и не выглядит как часть текста на мобильных устройствах",
            "- **Исправлен дубль AI-сообщений** в ЛС — добавлена дедупликация по ID: одно и то же сообщение не может появиться дважды",
            "- **AI-ассистент знает прогноз планов** — теперь при вопросе о планах AI видит: сколько дней прошло, ожидаемый % выполнения, прогноз к концу периода и статус (опережает / в темпе / отстаёт)",
            "Improve seller search to handle Cyrillic characters correctly",
            "Add instruction on how to create an Android TWA APK",
            "Add a guide for building Android TWA APKs and save it to the repository",
        ],
    },
    {
        "version": "1.131.0",
        "date": "21 июня 2026",
        "items": [
            "Expand AI assistant capabilities with new tools and improved data handling",
        ],
    },
    {
        "version": "1.130.0",
        "date": "21 июня 2026",
        "items": [
            "Умнее AI-ассистент: 20 инструментов вместо 15, анализ трендов по дням, сравнение периодов, разбивка по категориям и продавцам — теперь отвечает на вопросы о прошлой неделе и любых датах.",
            "Fix error preventing AI chat from refreshing its conversation history",
            "Update AI system prompt to include sales data retrieval",
        ],
    },
    {
        "version": "1.129.0",
        "date": "21 июня 2026",
        "items": [
            "Add customizable shift reminder settings to the web interface",
        ],
    },
    {
        "version": "1.128.0",
        "date": "21 июня 2026",
        "items": [
            "Add ability to customize shift reminder times for user notifications",
        ],
    },
    {
        "version": "1.127.0",
        "date": "21 июня 2026",
        "items": [
            "Improve search results display for personal chat messages",
        ],
    },
    {
        "version": "1.126.0",
        "date": "20 июня 2026",
        "items": [
            "Improve search functionality for personal chat messages",
        ],
    },
    {
        "version": "1.125.0",
        "date": "20 июня 2026",
        "items": [
            "Fix critical bugs and update admin functionality",
            "Improve search for personal chat messages with Cyrillic characters",
        ],
    },
    {
        "version": "1.124.0",
        "date": "19 июня 2026",
        "items": [
            "Improve AI and admin features with critical bug fixes",
        ],
    },
    {
        "version": "1.123.0",
        "date": "19 июня 2026",
        "items": [
            "Improve AI features and task management with critical bug fixes",
        ],
    },
    {
        "version": "1.122.0",
        "date": "19 июня 2026",
        "items": [
            "Add features for time tracking, task watchers, and AI-generated templates",
        ],
    },
    {
        "version": "1.121.0",
        "date": "19 июня 2026",
        "items": [
            "✨ AI для задач (Phase 3.4–3.7): AI-создание задачи из бота, AI-предиктор просрочки (11:00 UTC), еженедельный AI-дайджест задач (вторник 08:00), AI-анализ выполнения на странице задачи",
        ],
    },
    {
        "version": "1.120.0",
        "date": "19 июня 2026",
        "items": [
            "✨ AI-помощник в задачах (tasks_ai): кнопки «AI-описание» и «AI-чеклист» в форме создания задачи; страница декомпозиции — превратите цель в список задач одним кликом",
        ],
    },
    {
        "version": "1.119.0",
        "date": "19 июня 2026",
        "items": [
            "Задачи: при нажатии «Я выполнил» можно оставить краткий отчёт — сохраняется как комментарий; у каждого вложения теперь виден автор и дата загрузки",
        ],
    },
    {
        "version": "1.118.0",
        "date": "19 июня 2026",
        "items": [
            "Задачи: поиск по названию и описанию — строка поиска в шапке списка, кириллица поддерживается, фильтры и тема сохраняются при поиске",
        ],
    },
    {
        "version": "1.117.0",
        "date": "19 июня 2026",
        "items": [
            "Задачи (бот): просроченность теперь учитывает время дедлайна; при командном выполнении — авто-переход «На проверку» как в вебе; повторяющиеся задачи: правильный расчёт следующего месяца и копирование чеклиста",
            "Improve team task progress tracking and overdue deadline calculations",
        ],
    },
    {
        "version": "1.116.0",
        "date": "19 июня 2026",
        "items": [
            "Задачи: прогресс команды теперь показывает время выполнения каждого участника, сотрудники видят прогресс коллег, при 100% задача автоматически переходит на проверку; дедлайн с точностью до минуты учитывается при расчёте просрочки",
            "Update system version and deployment details",
            "Add screenshot of mobile deadline picker",
            "Show individual team member task completion details",
        ],
    },
    {
        "version": "1.115.0",
        "date": "19 июня 2026",
        "items": [
            "Update system version and deployment information",
            "Re-fetch AI hint when product filter changes in the plan edit form",
            "Show which filter is active in the AI hint result text",
        ],
    },
    {
        "version": "1.114.0",
        "date": "19 июня 2026",
        "items": [
            "Improve plan creation and editing with AI suggestions",
            "Re-fetch AI hint when seller or period changes in the edit form",
        ],
    },
    {
        "version": "1.113.0",
        "date": "19 июня 2026",
        "items": [
            "Update plan creation and editing with AI-powered suggestions",
            "auto-load AI plan hint on edit form open (task-77)",
        ],
    },
    {
        "version": "1.112.0",
        "date": "19 июня 2026",
        "items": [
            "AI-подсказка цели при создании плана продаж (полная реализация)",
        ],
    },
    {
        "version": "1.111.0",
        "date": "19 июня 2026",
        "items": [
            "AI-подсказка цели при создании плана продаж (с правками ревью)",
        ],
    },
    {
        "version": "1.110.0",
        "date": "19 июня 2026",
        "items": [
            "AI-подсказка цели при создании плана продаж",
        ],
    },
    {
        "version": "1.109.0",
        "date": "19 июня 2026",
        "items": [
            "AI-подсказка цели при создании плана продаж: на основе истории за последние периоды AI рекомендует реалистичный диапазон и кнопку «Применить» — в веб-форме и в боте.",
            "AI для товаров и цен — три новые AI-фичи",
        ],
    },
    {
        "version": "1.108.0",
        "date": "19 июня 2026",
        "items": [
            "Три AI-фичи для товаров: семантический поиск по каталогу, анализ совместных покупок (cross-sell) с советами по допродаже, AI-рекомендация по цене на основе скорости продаж за 30 и 90 дней.",
            "Еженедельные AI-советники — закупки/неликвид + коуч продавцу (v1.106–1.107)",
        ],
    },
    {
        "version": "1.107.0",
        "date": "19 июня 2026",
        "items": [
            "Еженедельные AI-советники — закупки/неликвид + коуч продавцу",
        ],
    },
    {
        "version": "1.106.0",
        "date": "19 июня 2026",
        "items": [
            "- **Советник по закупкам и неликвиду** — каждую субботу в 09:00 МСК владелец получает в Telegram отчёт: какие товары на грани нуля, что нужно пополнить в течение 2 недель, сколько денег заморожено в залежалых позициях, и AI-рекомендацию по действиям",
            "- **Персональный коуч продавцу** — каждую субботу в 09:00 МСК каждый продавец получает личное сообщение: своё место в рейтинге команды, выручку и средний чек в сравнении с командой, AI-совет по точке роста на следующую неделю",
            "- Уведомления также дублируются через Web Push и email (если включено в настройках AI-инсайтов)",
            "- Гейт: расширение AI-алертов в биллинге организации",
            "Root-cause алерты + утренний AI-брифинг в чат (v1.104–1.105)",
        ],
    },
    {
        "version": "1.105.0",
        "date": "19 июня 2026",
        "items": [
            "Root-cause алерты + утренний AI-брифинг в чат",
        ],
    },
    {
        "version": "1.104.0",
        "date": "19 июня 2026",
        "items": [
            "- **Root-cause в AI-алертах**: при падении выручки AI теперь называет конкретную категорию и продавца, внёсших наибольший вклад в спад — разбивка за вчера добавлена в промпт",
            "- **Утренний брифинг в AI-чат**: каждое утро в 10:00 МСК орги с расширением «AI-ассистент чата» получают автоматический брифинг за прошлый день прямо в AI-тему чата — выручка vs. норма, топ-категории, лидеры продаж, приоритет на сегодня",
            "Improve AI assistant responses with better prompts and adaptive settings",
        ],
    },
    {
        "version": "1.103.0",
        "date": "19 июня 2026",
        "items": [
            "Add cost tracking for AI features and improve reliability",
        ],
    },
    {
        "version": "1.102.0",
        "date": "19 июня 2026",
        "items": [
            "Add direct messaging shortcut to staff list",
        ],
    },
    {
        "version": "1.101.0",
        "date": "19 июня 2026",
        "items": [
            "Update dashboard to automatically filter sales and revenue by date and shop",
            "Add AI billing management and dashboard filtering capabilities",
            "Update pricing model to a two-layer system",
            "Fix error when marking bonus columns in the integration handler",
            "Add DM button to employee card in web cabinet (Task #64)",
            "Send push notification to employees at their shift start time",
        ],
    },
    {
        "version": "1.100.0",
        "date": "19 июня 2026",
        "items": [
            "Update dashboard to automatically filter sales by today's date",
        ],
    },
    {
        "version": "1.99.0",
        "date": "19 июня 2026",
        "items": [
            "Карточка «Продажи сегодня» и кнопка «Все →» на дашборде теперь открывают список продаж с автоматически подставленной датой — сразу видны сегодняшние транзакции без ручного выбора периода.",
            "Add AI usage tracking and display for users on their dashboard",
        ],
    },
    {
        "version": "1.98.0",
        "date": "19 июня 2026",
        "items": [
            "AI-биллинг этап 3: виджет AI-квоты на дашборде — пользователи видят прогресс-бар с дневным использованием AI (X / Y запросов, осталось N, цвет по заполненности); прунинг per-org счётчиков — ai_org_usage_log теперь также очищается ежедневно в 03:30 UTC вместе с ai_usage_log.",
        ],
    },
    {
        "version": "1.97.0",
        "date": "19 июня 2026",
        "items": [
            "AI-биллинг этап 2: per-org квота чат-ИИ — каждая организация теперь имеет независимый дневной счётчик AI-запросов (не разделяет пул с другими орг владельца); индивидуальный лимит пользователя — суперадмин может задать кастомный лимит для любого Telegram ID через /admin/ai-limits; реал-тайм баннер аномалий — предупреждение показывается прямо на странице когда сегодняшние запросы превысили 80% порога.",
        ],
    },
    {
        "version": "1.96.0",
        "date": "19 июня 2026",
        "items": [
            "AI-биллинг: постоянная история расходов — токены и стоимость теперь сохраняются в БД (таблица ai_cost_log) и не сбрасываются при рестарте сервера; в /admin/ai-limits добавлена карточка с 30-дневным графиком расходов по провайдерам; лимиты запросов кэшируются 60 секунд (снижение нагрузки на shop_bot.db); ежедневная автоочистка ai_cost_log (03:35 UTC, хранение 90 дней).",
            "Fix state clearing and rate limiting issues",
        ],
    },
    {
        "version": "1.95.0",
        "date": "19 июня 2026",
        "items": [
            "Комплексный аудит: исправлены нарушения порядка fsm_edit/clear в хендлерах комиссий и биллинга (clear вызывался до fsm_edit — anchor message не находился); rate-limit в sales-feed переведён с IP-адреса на telegram_id (за proxy Amvera у всех пользователей один IP — лимит блокировал всех разом).",
            "Improve AI assistant functionality and performance",
        ],
    },
    {
        "version": "1.94.0",
        "date": "19 июня 2026",
        "items": [
            "Исправлены баги в AI-ассистенте: топ-категория в сетевом анализе теперь отображается корректно (был неверный SQL); сжатие сессии больше не сжигает дневной лимит пользователя и работает быстрее (убраны лишние описания инструментов); при зависании инструментов AI теперь формирует финальный ответ вместо «недоступен»; описания инструментов кэшируются; таблицы rate-store создаются один раз при старте.",
            "Update release information and version numbers",
        ],
    },
    {
        "version": "1.93.0",
        "date": "19 июня 2026",
        "items": [
            "Add preset preview and QR code customization for product labels",
            "Fix AI assistant answering for the wrong month (inject current date)",
        ],
    },
    {
        "version": "1.92.0",
        "date": "19 июня 2026",
        "items": [
            "- Конструктор ценников: предпросмотр пресета перед применением. Нажмите 👁 у сохранённого пресета — оформление мгновенно покажется на живом превью без сохранения. Понравилось — «Применить», нет — «Отмена» вернёт прежний дизайн.",
        ],
    },
    {
        "version": "1.91.0",
        "date": "19 июня 2026",
        "items": [
            "- Конструктор ценников: настраиваемое содержимое QR-кода. Теперь в QR можно зашить ссылку или текст с подстановкой данных товара — плейсхолдеры {article}, {barcode}, {name}, {price}, {id}. Если поле пустое, QR кодирует артикул или штрихкод, как раньше.",
        ],
    },
    {
        "version": "1.90.0",
        "date": "19 июня 2026",
        "items": [
            "- Ценники: больше размеров и готовые раскладки листа — мини 30×20 и крупный 60×40 мм для термопринтеров, плюс две раскладки для обычной A4-самоклейки (24 и 65 ценников на лист).",
        ],
    },
    {
        "version": "1.89.0",
        "date": "19 июня 2026",
        "items": [
            "- Ценники: кнопка «Печать всех по фильтру» — печатайте ценники сразу для всех товаров категории или поискового запроса, а не только для выбранных на текущей странице (до 200 за раз).",
            "Add ability to save and apply custom label design presets",
        ],
    },
    {
        "version": "1.88.0",
        "date": "19 июня 2026",
        "items": [
            "- Конструктор ценников: библиотека именованных пресетов дизайна — сохраняйте текущее оформление как пресет, применяйте, переименовывайте и удаляйте. Логотип, шрифт, тема, порядок и видимость элементов сохраняются вместе с пресетом.",
            "Add support for displaying old prices on product labels and improve barcode formatting",
        ],
    },
    {
        "version": "1.87.0",
        "date": "19 июня 2026",
        "items": [
            "- На ценниках теперь можно показать старую (зачёркнутую) цену для акций: укажите «Старую цену» в карточке товара — на ценнике она появится перечёркнутой над текущей ценой (и на экране, и в PDF)",
        ],
    },
    {
        "version": "1.86.0",
        "date": "19 июня 2026",
        "items": [
            "- Штрих-коды на ценниках теперь печатаются в розничном стандарте EAN-13 (или EAN-8), если штрих-код товара — это 12–13 цифр; для остальных кодов по-прежнему Code-128",
        ],
    },
    {
        "version": "1.85.0",
        "date": "19 июня 2026",
        "items": [
            "- Ценники в PDF теперь корректно печатают русский текст (раньше названия могли пропадать)",
            "- PDF-ценник в точности повторяет порядок блоков, который вы задали в конструкторе",
            "- В PDF применяются выбранные шрифт (с засечками / без / моноширинный) и размер шрифта",
            "Add drag-and-drop functionality to reorder price tag elements on the design preview",
        ],
    },
    {
        "version": "1.84.0",
        "date": "19 июня 2026",
        "items": [
            "- Теперь порядок элементов можно менять **прямо на макете ценника** — зажмите блок мышкой (или пальцем на телефоне) и перетащите вверх/вниз",
            "- Боковая панель «Элементы» и макет синхронизированы — меняете в одном месте, обновляется везде",
            "- Подсветка блока при наведении подсказывает, что его можно двигать",
            "- Новый порядок сохраняется кнопкой «Сохранить дизайн»",
            "Enhance price tag constructor with barcode, theme, and drag-and-drop features",
        ],
    },
    {
        "version": "1.83.0",
        "date": "19 июня 2026",
        "items": [
            "- **Штрих-код Code-128** прямо на ценнике (JsBarcode.js) — сканируется любым считывателем",
            "- **Управление элементами**: включить/выключить логотип, значок акции, QR, штрихкод, категорию, описание, артикул — одним чекбоксом",
            "- **Drag & drop порядка элементов** — перетащите блоки в нужном порядке, всё сохраняется",
            "- **4 готовые темы**: Стандарт, Тёмная, Акцент, Минимал — применяются мгновенно",
            "- **Полный дизайн-редактор**: цвет фона, текста, цены, рамки; толщина рамки; шрифт (4 варианта); значок акции",
            "- **Копии ценников** — укажите сколько штук печатать для каждого товара (1–99)",
            "- **Торговые сети**: выберите сеть — подставятся её артикулы и штрихкоды",
            "- **PDF-экспорт** отражает дизайн — цвета, логотип, штрихкод, видимые элементы",
            "- Дизайн сохраняется на сервере кнопкой «Сохранить дизайн»",
            "Add ability to assign unique product codes for different retail chains",
        ],
    },
    {
        "version": "1.82.0",
        "date": "19 июня 2026",
        "items": [
            "- Один товар — разные артикул и штрихкод по торговым сетям: в карточке товара (бот и веб) теперь можно задать отдельные коды для каждой сети (например, свой штрихкод в DNS и другой в М-Видео). Сканирование в кассе находит товар по коду нужной сети, а отчёты и рейтинги остаются по одному товару без дублей. Если код для сети не задан — используется код по умолчанию.",
        ],
    },
    {
        "version": "1.81.0",
        "date": "19 июня 2026",
        "items": [
            "Implement direct APK binary upload from GitHub Actions to server",
        ],
    },
    {
        "version": "1.80.0",
        "date": "19 июня 2026",
        "items": [
            "Fix app download by using the correct GitHub asset API URL",
        ],
    },
    {
        "version": "1.79.0",
        "date": "19 июня 2026",
        "items": [
            "Update APK download to exclusively use Amvera links",
        ],
    },
    {
        "version": "1.78.0",
        "date": "18 июня 2026",
        "items": [
            "Fix APK download: 200KB size threshold + GitHub auth for private repo",
        ],
    },
    {
        "version": "1.77.0",
        "date": "18 июня 2026",
        "items": [
            "Update application version and improve absence calendar features",
            "Highlight heavy-absence days on the admin absence calendar",
            "Let admins mute the heavy-absence alert for a specific day",
        ],
    },
    {
        "version": "1.76.0",
        "date": "18 июня 2026",
        "items": [
            "Fix absence calendar display and improve navigation",
            "Show hover tooltip on absences calendar for pending absences too",
            "Show tooltip with all absent employees on the same calendar day",
            "Highlight calendar days with multiple absences using a distinct visual style",
            "Add heavy-absence-day warning when 3+ people are out at once",
        ],
    },
    {
        "version": "1.75.0",
        "date": "18 июня 2026",
        "items": [
            "Fix calendar displays and improve absence navigation",
        ],
    },
    {
        "version": "1.74.0",
        "date": "18 июня 2026",
        "items": [
            "Improve calendar views with detailed absence information and consistent tooltips",
            "Show absence tooltips in the admin absences calendar view",
            "Add screenshot to the project assets",
        ],
    },
    {
        "version": "1.73.0",
        "date": "18 июня 2026",
        "items": [
            "Improve calendar view with absence details and tooltips",
            "Show absence legend only when employee has absences in that month",
            "Show a tooltip with absence dates on hover in the calendar",
        ],
    },
    {
        "version": "1.72.0",
        "date": "18 июня 2026",
        "items": [
            "Highlight absence days in salary and staff shift calendars",
        ],
    },
    {
        "version": "1.71.0",
        "date": "18 июня 2026",
        "items": [
            "Отпуска и больничные теперь подсвечиваются прямо в календаре смен — 🌴 синим, 🤒 жёлтым. Работает на страницах зарплаты и карточки сотрудника.",
            "Еженедельный и ежемесячный рейтинг продавцов теперь приходит в бот с кнопкой «🌐 Рейтинг в вебе» — открывает веб-кабинет сразу на нужном периоде.",
            "Update system to correctly display sales and absence data",
            "Let managers jump from a sales report directly to a product's detail page scoped to that month",
            "Deep-link from bot ranking/report messages into web staff profiles",
            "Allow managers to jump to a staff member's web page for a past month",
            "Add web deep-links to bot dashboard reports and daily summaries",
            "Absence calendar — month-aware sidebar badges",
            "Add weekly/monthly ranking notifications with 'Open in web' button",
            "add per-seller deep-links in admin daily report (task-36)",
        ],
    },
    {
        "version": "1.70.0",
        "date": "18 июня 2026",
        "items": [
            "Клик по продавцу в рейтинге, отчётах и дашборде теперь открывает его профиль сразу за нужный месяц",
            "Update system to include paid time off in salary and absence calculations",
            "Show absence status badge on absences admin page employee list",
            "Show absence status badge on the salary page for each employee",
            "Show absence badge in absences list records",
            "Show today-absent badge in the absence calendar view",
            "Show absence badge on salary page for past months too",
            "Show absence type summary in the salary detail panel for managers",
            "Show absence badge on the staff page when viewing past months",
            "Show absence badges on staff detail page when viewing past months",
            "show correct monthly sales stats when browsing past months on employee detail page",
            "scope recent-sales list on employee detail page to the viewed month",
            "Add ?year=YYYY&month=MM params to all staff detail links from monthly contexts",
        ],
    },
    {
        "version": "1.69.0",
        "date": "18 июня 2026",
        "items": [
            "Update system to include paid time off in salary and absence calculations",
            "Show paid absence days in bot salary adjustments detail screen",
            "Show absence badge on the individual staff card page too",
        ],
    },
    {
        "version": "1.68.0",
        "date": "18 июня 2026",
        "items": [
            "Сотрудники в одобренном отпуске или на больничном:",
            "- **Не отображаются** в блоке «На смене сегодня» (дашборд веб и бот)",
            "- На **карточке сотрудника** появляется badge «🌴 В отпуске» / «🤒 На больничном» с датой окончания",
            "- **Зарплата** на карточке считается правильно: оплачиваемые дни отсутствия учитываются как отработанные (`(смены + оплач. отсутствий) × ставка`)",
            "- **График работы**: под счётчиком рабочих дней показывается уточнение «−N в отпуске», если одобренное отсутствие перекрывает рабочие дни",
            "Расчёт зарплаты в боте теперь учитывает оплачиваемые дни отпуска: формула `(смены + оплач. отпуск) × ставка` — как в веб-карточке сотрудника. При наличии отпускных дней показывается разбивка: «Смен: 18 + оплач. отпуск: 4 = 22».",
        ],
    },
    {
        "version": "1.67.0",
        "date": "17 июня 2026",
        "items": [
            "AI-дайджест сети теперь отправляется на email владельца, если включена опция «Email-дайджест» в настройках AI-инсайтов и добавлен подтверждённый адрес почты.",
            "Update application with email alerts and real-time insights",
            "Add email delivery to the AI network insights digest for owners",
            "Show warning when email alerts on but no admin has verified email",
        ],
    },
    {
        "version": "1.66.0",
        "date": "17 июня 2026",
        "items": [
            "Email-доставка AI-алертов и дайджестов: в настройках инсайтов появились переключатели «Email для алертов» и «Email для дайджеста» — письма отправляются администраторам с привязанным и подтверждённым email.",
            "Update application to include AI insights history and settings",
            "Перенести настройки AI-алертов со страницы настроек на /ai-insights",
            "real-time alert history refresh on /ai-insights without page reload",
            "Добавить email-доставку умных алертов и дайджестов",
        ],
    },
    {
        "version": "1.65.0",
        "date": "17 июня 2026",
        "items": [
            "- **AI-инсайты**: история алертов и дайджестов теперь хранится в БД и отображается прямо на странице `/ai-insights` — без ожидания Telegram",
            "- **AI-инсайты**: настройки инсайтов (порог, час рассылки, метрики, контекст дайджеста, web push) вынесены на страницу `/ai-insights` для владельцев и администраторов",
            "Add ability to edit and customize export rules directly on the website",
            "AI-инсайты везде + настройки для оргадминов",
        ],
    },
    {
        "version": "1.64.0",
        "date": "17 июня 2026",
        "items": [
            "Интеграции: редактирование правил экспорта прямо в веб-кабинете — кнопка ✏️ у каждого правила, поддержка произвольного cron-расписания и настройка маппинга матрицы для операции «Обновить ячейку».",
            "Add WebSocket library to support real-time features",
            "Add screenshots for integration page and bot functionality",
        ],
    },
    {
        "version": "1.63.0",
        "date": "17 июня 2026",
        "items": [
            "Update landing page and version with absolute download links",
        ],
    },
    {
        "version": "1.62.0",
        "date": "17 июня 2026",
        "items": [
            "Update landing page to use absolute download links",
            "Expand Integrations page — full CRUD for export rules, motiv config form, edit/test connection",
        ],
    },
    {
        "version": "1.61.0",
        "date": "17 июня 2026",
        "items": [
            "Restore landing page to previous version with emoji icons",
        ],
    },
    {
        "version": "1.60.0",
        "date": "17 июня 2026",
        "items": [
            "Fix sidebar display issue causing content to shift down",
            "Extend premium 3D icons to all remaining landing icons",
        ],
    },
    {
        "version": "1.59.0",
        "date": "17 июня 2026",
        "items": [
            "Update deployment scripts and application version for security",
        ],
    },
    {
        "version": "1.58.0",
        "date": "17 июня 2026",
        "items": [
            "Update application version and deployment details",
        ],
    },
    {
        "version": "1.57.0",
        "date": "17 июня 2026",
        "items": [
            "Update competitive analysis report with new metrics and features",
            "Full \"Precision Grid\" redesign of web/templates/landing.html",
            "Restored to '8a51129379bbd088ae145d701b1b1da91befa67f'",
            "Add real OG screenshot image for social sharing previews",
            "redesign landing.html to \"Тёплая Точность\" visual concept",
            "Premium landing page redesign — Vercel/Linear quality level",
            "Restored to 'b12abb6870149eb947f4b1ce104340c67b37b9c1'",
            "Saved your changes before starting work",
            "Restore landing page to a previous version",
            "Update landing page to include new features and improve design elements",
            "Add screenshots to asset directory",
        ],
    },
    {
        "version": "1.56.0",
        "date": "17 июня 2026",
        "items": [
            "Improve security and prevent data loss by fixing database connection leaks and escaping user input",
        ],
    },
    {
        "version": "1.55.0",
        "date": "17 июня 2026",
        "items": [
            "Prevent beta bar from overlapping with the website header",
        ],
    },
    {
        "version": "1.54.0",
        "date": "17 июня 2026",
        "items": [
            "Update landing page text from \"without card\" to \"without payment\"",
        ],
    },
    {
        "version": "1.53.0",
        "date": "17 июня 2026",
        "items": [
            "Update landing page content and fix a database connection issue",
        ],
    },
    {
        "version": "1.52.0",
        "date": "17 июня 2026",
        "items": [
            "Improve database connection handling and optimize data retrieval operations",
        ],
    },
    {
        "version": "1.51.0",
        "date": "17 июня 2026",
        "items": [
            "Оптимизация: xlsx-экспорт зарплат теперь делает 3 SQL-запроса вместо 4×N (новый метод get_salary_xlsx_bulk); страница /admin/orgs загружает счётчики сотрудников за 1 запрос вместо N (get_all_org_user_counts).",
            "Add HTML escaping to prevent injection in notifications",
        ],
    },
    {
        "version": "1.50.0",
        "date": "17 июня 2026",
        "items": [
            "Закрыта HTML-инъекция в Telegram-уведомлениях из модулей задач (tasks.py — 9 точек: title, task['title']), отсутствий (absences.py — admin_comment) и подписок (subscription.py — user_display, label).",
        ],
    },
    {
        "version": "1.49.0",
        "date": "17 июня 2026",
        "items": [
            "- Веб-дашборд: 30 запросов к БД заменены одним GROUP BY (30-дневный график)",
            "- Страница сотрудников: N+1 запросы заменены одним bulk-запросом",
            "- Обратная связь: HTML-экранирование всех полей перед отправкой в Telegram",
            "- Устранены утечки соединений с БД в categories, inventory, dashboard, sales, salary",
            "Improve performance by optimizing data fetching and error handling",
        ],
    },
    {
        "version": "1.48.0",
        "date": "17 июня 2026",
        "items": [
            "- Производительность страницы зарплаты: оплачиваемые дни отсутствия теперь загружаются одним запросом для всех сотрудников (вместо N запросов); то же в Excel-экспорте зарплаты",
        ],
    },
    {
        "version": "1.47.0",
        "date": "17 июня 2026",
        "items": [
            "- Аудит стабильности (этап 5): при подписке на Web Push убран лишний вызов `create_tables()` на каждый запрос — миграции уже выполнены при старте; логирование ошибок во всех JSON API push-эндпоинтах теперь явное",
        ],
    },
    {
        "version": "1.46.0",
        "date": "17 июня 2026",
        "items": [
            "- Аудит безопасности (этап 4): устранена утечка внутренних деталей ошибок (SQLite-пути, имена таблиц) через HTTP-ответы — 13 мест в 6 файлах заменены на дженерик-сообщения с логированием; оптимизация дашборда: счётчики товаров и сотрудников теперь через SELECT COUNT(*) вместо загрузки всех строк в Python",
            "Finalize comprehensive audit by addressing stability and performance issues",
        ],
    },
    {
        "version": "1.45.0",
        "date": "17 июня 2026",
        "items": [
            "- Аудит производительности (этап 3б): страница товаров больше не загружает все строки инвентаря в Python — новый метод `get_stock_totals()` делает один SQL GROUP BY запрос и возвращает `{product_id: qty}`; при больших каталогах — кратное снижение памяти и времени отклика",
        ],
    },
    {
        "version": "1.44.0",
        "date": "17 июня 2026",
        "items": [
            "- Аудит производительности (этап 3): фильтрация продаж по продавцу и товару перенесена из Python в SQL — `get_sales_report` получил параметры `user_id` и `product_id`; фильтрация по категории в отчётах теперь тоже в SQL; при тысячах строк — экономия RAM и задержки на страницах продаж и аналитики",
        ],
    },
    {
        "version": "1.43.0",
        "date": "17 июня 2026",
        "items": [
            "- Аудит безопасности (этап 2): укреплена CSRF-защита AI API-роутов — теперь требуется X-Requested-With: XMLHttpRequest или совпадение Origin (вместо беззащитного \"нет Origin → пропустить\"); все 6 fetch-вызовов к AI API в шаблонах получили заголовок; проактивная очистка import-сессий при загрузке страницы товаров",
        ],
    },
    {
        "version": "1.42.0",
        "date": "17 июня 2026",
        "items": [
            "- Аудит безопасности и надёжности (этап 1): исправлена миграция `plan_milestone_alerts` (ALTER TABLE вместо DROP TABLE — данные больше не теряются), добавлен биллинговый гейт на страницу гибкой оргструктуры, исправлены утечки соединений SQLite в db_utils (try-finally), устранена утечка памяти в rate-limiter (очистка устаревших IP), добавлены индексы `product_id` и `product_id+sale_date` для ускорения аналитики товаров",
            "Improve AI safety and reliability with critical fixes and new features",
        ],
    },
    {
        "version": "1.41.0",
        "date": "17 июня 2026",
        "items": [
            "Improve AI safety and reliability with critical fixes",
            "AI extensions в БД + очистка ai_usage_log",
            "Fix AI-инсайты 500 на продакшне (Starlette TemplateResponse API)",
            "AI: единый лимит чата из UI",
            "AI kill-switch + алерт аномалий + трекинг токенов",
        ],
    },
    {
        "version": "1.40.0",
        "date": "16 июня 2026",
        "items": [
            "Improve swipe gesture responsiveness on sales module",
            "Add screenshots to document AI feature limitations and usage",
        ],
    },
    {
        "version": "1.39.0",
        "date": "16 июня 2026",
        "items": [
            "Update chat interface with improved AI hints and dynamic search functionality",
            "Make AI error messages visually distinct from normal AI replies in chat",
        ],
    },
    {
        "version": "1.38.0",
        "date": "16 июня 2026",
        "items": [
            "Add AI session management and auto-archiving features to the chat",
            "Let admins configure AI usage history retention period",
            "Filter AI tool usage chart by individual tool or date range",
            "Show AI tool usage broken down by organisation on the admin page",
            "Remove duplicate 🤖 emoji from AI chat messages",
        ],
    },
    {
        "version": "1.37.0",
        "date": "16 июня 2026",
        "items": [
            "Add AI conversation management and auto-archiving features",
        ],
    },
    {
        "version": "1.36.0",
        "date": "16 июня 2026",
        "items": [
            "- Кнопка **«Новый диалог»** (⟳) в AI ЛС и AI-теме — явный сброс контекста; история остаётся в базе",
            "- Кнопка прикрепить файл скрыта в AI-треде (там файлы не нужны)",
            "- **Авто-сжатие**: при >12 сообщений в сессии AI делает краткое резюме и продолжает с ним",
            "- **Авто-архивация**: сессии без активности >30 дней автоматически закрываются (ежедневно в 03:20 UTC)",
            "Add conversation history to AI assistant chats and direct messages",
            "Add AI chat topic \"thinking\" indicator",
            "Track AI tool usage with in-memory counters + admin endpoint",
            "Show AI tool usage in the super-admin dashboard UI",
            "Persist AI tool usage stats across server restarts",
            "Add nightly background job to prune old AI tool usage records",
            "Show AI tool usage history charts in the super-admin dashboard",
        ],
    },
    {
        "version": "1.35.0",
        "date": "16 июня 2026",
        "items": [
            "Чат с AI-ассистентом: вопросы теперь всегда доходят до сервера и запускают генерацию ответа; пока ИИ думает — виден индикатор «🤖 ИИ думает…»; ответ появляется в открытом диалоге мгновенно без перезагрузки страницы.",
            "Fix chat layout to prevent scrolling and ensure fixed elements stay in place",
            "Fix AI chat: realtime replies + thinking indicator",
            "AI assistant full org data access via 15-tool function-calling layer",
        ],
    },
    {
        "version": "1.34.0",
        "date": "16 июня 2026",
        "items": [
            "Move AI assistant to a dedicated chat topic and improve validation",
        ],
    },
    {
        "version": "1.33.0",
        "date": "16 июня 2026",
        "items": [
            "- AI-ассистент в общем чате переехал в отдельную тему «🤖 AI-ассистент»: любое сообщение в ней — это вопрос к AI, ответ виден всей команде. Префикс «@ии» в обычных темах больше не нужен и убран.",
            "Separate AI assistant into its own chat thread and improve validation",
        ],
    },
    {
        "version": "1.32.0",
        "date": "16 июня 2026",
        "items": [
            "- AI-ассистент теперь отдельный собеседник в личных сообщениях: у каждого сотрудника свой приватный чат с AI (значок 🤖 вверху списка диалогов).",
            "- В обычных личных переписках с коллегами AI больше не вмешивается — хук «@ии» убран; общение с людьми остаётся только между людьми.",
            "Fix unread message counter for AI assistant replies in direct messages",
            "Add HTTP-level tests for POST /chat/send and POST /chat/dm/send (test #12)",
            "Prevent chat messages from disappearing when push/WS fails",
        ],
    },
    {
        "version": "1.31.0",
        "date": "16 июня 2026",
        "items": [
            "Исправлен «вечный» счётчик непрочитанных в личных сообщениях: ответы AI-ассистента (отправитель — система) теперь корректно помечаются прочитанными при открытии диалога и привязаны к строке нужного собеседника.",
            "Update chat module with corrected type annotations and audit details",
            "Add HTTP end-to-end tests for chat mark-read routes",
        ],
    },
    {
        "version": "1.30.0",
        "date": "16 июня 2026",
        "items": [
            "Аудит модуля чата: type-аннотации rate-limit хранилищ приведены к `int` (telegram_id); все 7 DB-индексов чата подтверждены; 10/10 функциональных тестов.",
            "Update application with new version and improved notification features",
            "автотест — бейдж чата обнуляется после прочтения",
        ],
    },
    {
        "version": "1.29.0",
        "date": "16 июня 2026",
        "items": [
            "Improve notification system by adding missing emojis and fixing history tracking",
            "Чат — FAB-бейдж не обнулялся + поле ввода DM плыло",
        ],
    },
    {
        "version": "1.28.0",
        "date": "16 июня 2026",
        "items": [
            "- Шторка уведомлений: правильные эмодзи для DM-сообщений (💬), зарплатных корректировок (💵) и уведомлений о продажах (💰)",
            "- AI-алерты и дайджесты теперь отображаются в колокольчике (история не писалась)",
            "- Уведомления об отсутствиях теперь видны в шторке колокольчика",
            "- Фикс: rate-limit счётчика непрочитанных по ID пользователя, а не по IP (на Amvera был общий IP)",
            "Update billing system and add unread chat indicator",
        ],
    },
    {
        "version": "1.27.0",
        "date": "16 июня 2026",
        "items": [
            "Модернизация бот-панели «💰 Платёжная система» под модульный биллинг веб /admin/billing",
            "show unread chat badge on desktop sidebar Чат nav item",
        ],
    },
    {
        "version": "1.26.0",
        "date": "16 июня 2026",
        "items": [
            "Чат: бейдж FAB по серверному read-state + анкеринг поля ввода",
        ],
    },
    {
        "version": "1.25.0",
        "date": "16 июня 2026",
        "items": [
            "Update project version and deployment information",
            "Add checks to ensure chat module is available when creating tasks",
        ],
    },
    {
        "version": "1.24.0",
        "date": "16 июня 2026",
        "items": [
            "Improve chat experience with persistent connections and layout adjustments",
        ],
    },
    {
        "version": "1.23.0",
        "date": "16 июня 2026",
        "items": [
            "Improve chat functionality with real-time updates and better error handling",
        ],
    },
    {
        "version": "1.22.0",
        "date": "16 июня 2026",
        "items": [
            "💬 Чат: непрочитанные сообщения теперь синхронизируются между всеми устройствами",
            "🗑️ Удалённое сообщение в личной переписке исчезает у собеседника сразу, без обновления страницы",
            "📎 Исправлены ссылки на вложения в личных сообщениях",
        ],
    },
    {
        "version": "1.21.0",
        "date": "16 июня 2026",
        "items": [
            "Update deployment details and version for recent fixes",
            "Keep AI DM replies visible after page refresh",
        ],
    },
    {
        "version": "1.20.0",
        "date": "16 июня 2026",
        "items": [
            "chat header invisible in light mode (CDN Tailwind timing)",
            "AI DM replies invisible to users (@ии in DM never shows)",
        ],
    },
    {
        "version": "1.19.0",
        "date": "16 июня 2026",
        "items": [
            "Update deployment hashes and version with new features",
            "Git commit prior to merge",
            "Apply push notification toggle to daily AI alerts",
            "Mirror AI alert push toggle in bot's alert settings menu",
        ],
    },
    {
        "version": "1.18.0",
        "date": "16 июня 2026",
        "items": [
            "Add live product search and backdated sales filter functionality",
            "Let owners control whether the weekly AI digest sends a push notification",
        ],
    },
    {
        "version": "1.17.0",
        "date": "16 июня 2026",
        "items": [
            "Update application with new AI features and deployment information",
            "Show AI alert settings button in subscription/profile screen",
            "Let owners toggle AI alerts and adjust threshold/hour from the bot (Task #117)",
            "Live \"next digest\" preview on settings page",
        ],
    },
    {
        "version": "1.16.0",
        "date": "16 июня 2026",
        "items": [
            "AI-ассистент в общем чате теперь отображается с тем же стилем, что и в личных сообщениях: бирюзовая рамка, аватар 🤖 и имя «AI-ассистент» выделены зелёным цветом.",
            "Еженедельный AI-дайджест теперь отображается в веб-кабинете на странице AI-инсайтов — без перехода на другую страницу. Последний дайджест загружается сразу при открытии, кнопка «↻ обновить» подгружает свежие данные через Alpine без перезагрузки.",
            "Владельцы магазинов теперь могут выбрать день недели и время отправки еженедельного AI-дайджеста прямо в настройках — вместо фиксированного понедельника 09:00.",
            "Update application information and AI features for improved user experience",
            "Show the weekly AI digest in the web cabinet without leaving the page",
            "Show AI bot style in topic chat messages",
            "Let owners choose what day and time the weekly digest arrives",
            "Show AI context blocks in bot and allow inline toggle",
            "Send AI weekly digest via web push in addition to Telegram",
        ],
    },
    {
        "version": "1.15.0",
        "date": "16 июня 2026",
        "items": [
            "Update AI features and deployment information for the application",
            "Let owners configure which metrics appear in their AI digest",
            "Include category breakdown and sales trend in weekly digest",
            "Show AI replies with a distinct bot style in DM conversations",
            "Skip fetching context data the owner has disabled in ai_smart_alerts",
            "Show AI hint in DM input placeholder",
        ],
    },
    {
        "version": "1.14.0",
        "date": "16 июня 2026",
        "items": [
            "AI-ассистент теперь отвечает и в личных сообщениях: напишите `@ИИ` в DM — и получите ответ с данными организации (топ товаров, активные продавцы, прогресс планов). Контекст такой же богатый, как в топиках чата.",
            "Еженедельный AI-дайджест по понедельникам: топ-товары, лидеры продаж и выполнение планов — теперь приходят владельцам даже когда всё хорошо, не только при падениях.",
            "Update application with new AI features and improve deployment information",
            "Apply rich context to scheduled AI digest (weekly smart alerts)",
            "Show AI what each seller's plan completion looks like individually",
            "Extend AI context to DM conversations (Task #90)",
            "AI weekly positive digest — top products, sellers, plan progress every Monday",
        ],
    },
    {
        "version": "1.13.0",
        "date": "16 июня 2026",
        "items": [
            "Владельцы сети с ≥2 магазинами теперь могут подключить расширение **🌐 AI-инсайты сети** (399₽/мес) прямо из бота — через меню «➕ Надстройки».",
            "Show AI network insights widget on the web dashboard for multi-shop owners",
            "Cache AI plan analysis results to avoid redundant LLM calls",
            "Let owners choose what time they receive the weekly AI digest",
            "Let owners buy the AI network insights add-on directly from the bot",
            "Давать AI больше контекста: топ-продукты, активные планы, список сотрудников",
        ],
    },
    {
        "version": "1.12.0",
        "date": "16 июня 2026",
        "items": [
            "Новое расширение — **AI-ассистент в чате** 💬: напишите `@ИИ` в любом топике и получите ответ с реальными данными вашей организации (продажи за сегодня и месяц). Ответ приходит асинхронно, с суточным лимитом на запросы. Доступно как платное расширение `ai_chat_assistant` под модулем «ИИ-ассистент».",
            "Новое расширение «🔍 AI-разбор планов» — кнопка на странице Планов для планов с выполнением < 95%: AI анализирует причины невыполнения по дням, продавцам и категориям, выдаёт главную причину, проблемные зоны и рекомендации.",
            "Remove smooth navigation features and restore previous functionality",
            "AI-разбор причин невыполнения плана продаж",
            "Cross-org AI intelligence — AI network insights for multi-shop owners",
            "AI-ассистент в организационном чате (@ИИ в топиках)",
            "Add AI plan analysis button to plan detail page",
        ],
    },
    {
        "version": "1.11.0",
        "date": "16 июня 2026",
        "items": [
            "HTMX content-swap navigation (no full page reloads on menu clicks)",
            "Extend HTMX fast-nav to the 'More' sheet links on mobile",
            "Update browser tab title on HTMX navigation",
            "Cache HTMX page snapshots so back/forward feels instant",
            "Skip HTMX history caching for sensitive/live pages",
            "Stop dashboard from showing stale charts when navigating back",
        ],
    },
    {
        "version": "1.10.0",
        "date": "16 июня 2026",
        "items": [
            "Add ability to edit and manage motivation rules",
        ],
    },
    {
        "version": "1.9.0",
        "date": "16 июня 2026",
        "items": [
            "Оптимизация веб-кабинета: per-request кэш + bulk-биллинг (#1), Tailwind CDN → статический app.css (#3)",
            "Запускать тесты авто-сборки новостей при каждом деплое",
            "Покрыть тестами AI-полировку новостей (_polish_with_ai)",
        ],
    },
    {
        "version": "1.8.0",
        "date": "16 июня 2026",
        "items": [
            "Оптимизация веб-кабинета: per-request кэш + bulk-биллинг (#1), Tailwind CDN → статический app.css (#3)",
        ],
    },
    {
        "version": "1.7.0",
        "date": "16 июня 2026",
        "items": [
            "Consolidate styling and animations, improving performance and consistency",
        ],
    },
    {
        "version": "1.6.0",
        "date": "16 июня 2026",
        "items": [
            "📥 Импорт из Google Таблиц по кнопкам: выбираете строку-заголовок и колонки прямо из реальных данных таблицы, без ручного ввода номеров",
            "⚡ Быстрый повторный импорт: настройки сохраняются и запускаются снова одним нажатием",
            "🎯 Таргетированная мотивация: задавайте ставку за товар отдельно для сети, города, магазина или конкретного сотрудника — приоритет от частного к общему",
            "🏷 В зарплатных отчётах видно, какой уровень нацеливания начислил премию по каждой продаже",
            "📊 Сводка премии по уровням нацеливания (сеть, город, магазин, сотрудник) в зарплате и в выгрузке Excel",
            "🤖 Управление таргетированными правилами мотивации прямо в Telegram-боте — просмотр и удаление",
            "📈 В сводках заработка бота (за месяц, за год и за выбранный месяц) показывается разбивка премии по источнику мотивации",
        ],
    },
    {
        "version": "1.5.0",
        "date": "15 июня 2026",
        "items": [
            "🎨 Обновлён логотип DS CRM — единый стиль с белой подписью «CRM» во всём кабинете, на иконке приложения и в Android-версии",
        ],
    },
    {
        "version": "1.4.0",
        "date": "15 июня 2026",
        "items": [
            "📱 Иконка Android-приложения обновлена — теперь отображает «CRM» как на веб-версии",
        ],
    },
    {
        "version": "1.3.0",
        "date": "15 июня 2026",
        "items": [
            "✏️ Редактирование продажи прямо в окне — удаление, изменение даты, история изменений",
            "🔃 Сортировка таблиц по любому столбцу — продажи, товары, сотрудники, остатки, зарплата, рейтинги, отчёты",
            "💾 Запоминание выбранной сортировки и фильтров между визитами",
            "📅 Дата продажи в таблице + подсветка продаж «задним числом»",
            "🔎 Живой поиск товаров на странице остатков",
            "📋 История изменений продажи в Telegram-боте",
            "🎨 Обновлённый логотип с подписью «CRM»",
        ],
    },
    {
        "version": "1.2.0",
        "date": "14 июня 2026",
        "items": [
            "🏷 Артикулы товаров (SKU) — авто-генерация, ручной ввод, массовое присвоение",
            "🔍 Поиск по артикулу в каталоге, остатках, боте и кассе POS",
            "🖨 Печать ценников с QR-кодом (3 размера: 58×40, 40×30, A6)",
            "📄 Скачивание ценников как PDF-файл",
            "🎨 Кастомный дизайн ценников — цвета, логотип, размер шрифта",
            "📷 Сканирование штрих-кода при добавлении продажи",
            "🖥 Сканирование штрих-кода на кассе POS",
            "📥 Импорт артикулов из Excel-таблицы",
        ],
    },
    {
        "version": "1.1.0",
        "date": "1 июня 2026",
        "items": [
            "📱 Android APK — скачивание из бота и веб-кабинета",
            "🔔 Уведомления о выходе новых версий APK для владельцев",
            "📊 Статистика скачиваний APK в панели администратора",
            "🗺 Отслеживание источника перехода к скачиванию APK",
        ],
    },
    {
        "version": "1.0.0",
        "date": "1 января 2026",
        "items": [
            "🛒 Продажи, товары, остатки — мультимагазинный учёт",
            "👥 Сотрудники, роли, приглашения, оргструктура",
            "💰 Зарплата, оклады, мотивация, корректировки",
            "📅 График работы, шаблоны смен, отсутствия",
            "🏆 Планы продаж, конкурсы, рейтинги",
            "📊 Аналитика, дашборд, ABC-анализ, тепловая карта",
            "🔗 Google Sheets, Excel-импорт/экспорт",
            "💬 Внутренний чат (топики + личные сообщения)",
            "🔔 Push-уведомления, email/пароль вход, тёмная тема",
        ],
    },
]
