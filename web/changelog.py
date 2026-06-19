CURRENT_VERSION = "1.86.0"

ENTRIES = [
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
