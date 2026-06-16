CURRENT_VERSION = "1.22.0"

ENTRIES = [
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
