# UI MAP — Telegram Shop Bot

> Справочник для агентов. Описывает полную карту интерфейса по каждому типу пользователя,
> все callback_data, состояния FSM и разветвления логики.
> Обновлён: 2026-05-26

---

## Содержание

1. [Типы пользователей и определение роли](#1-типы-пользователей)
2. [Регистрация и вход](#2-регистрация-и-вход)
3. [Главное меню](#3-главное-меню)
4. [Суперадмин — Системная панель](#4-суперадмин--системная-панель)
5. [Управление организацией (owner / admin)](#5-управление-организацией)
   - 5.1 Товары (`products`)
   - 5.2 Остатки (`manage_inventory`)
   - 5.3 Продажи — редактирование (`edit_sales`)
   - 5.4 Мотивация/Комиссии (`admin_motivation`)
   - 5.5 Планы продаж (`admin_sales_plans`)
   - 5.6 Оклады и смены (`admin_salary_menu`)
   - 5.7 Конкурсы (`contests_menu`)
   - 5.8 Сотрудники (`admin_users`)
   - 5.9 Магазины (`admin_shops`)
   - 5.10 Google Sheets (`integration_menu`)
6. [Продажа (все роли)](#6-продажа)
7. [Отчёты и рейтинги](#7-отчёты-и-рейтинги)
8. [Профиль и настройки](#8-профиль-и-настройки)
9. [Уведомления](#9-уведомления)
10. [Подписки и оплата](#10-подписки-и-оплата)
11. [Личный кабинет (personal mode)](#11-личный-кабинет)
12. [FSM-состояния](#12-fsm-состояния)
13. [Ключевые callback_data — быстрый справочник](#13-быстрый-справочник-callback_data)

---

## 1. Типы пользователей

| Тип | Определение | DB | Доп. права |
|-----|------------|-----|------------|
| **Суперадмин** | `env_manager.is_super_admin(tid)` → `True` (telegram_id в env) | `data/main.db` | Видит «🔧 Системная панель», рассылает по всем БД |
| **Личное использование** | `ADMIN_CHAT_ID` env var совпадает с tid | `data/shop_bot.db` | Полный admin в своей личной БД |
| **Владелец орга (owner)** | `user_org_mapping.role = 'owner'` | `data/tenants/org_N.db` | Полный доступ к своей организации |
| **Администратор орга (admin)** | `user_org_mapping.role = 'admin'` + `scope_type/scope_value` | `data/tenants/org_N.db` | Ограничен своим scope (магазин / город / сеть) |
| **Сотрудник (user)** | `user_org_mapping.role = 'user'` | `data/tenants/org_N.db` | Только продажи, свои данные, остатки своего магазина |

**Функции определения:**
- `is_any_admin(tid)` → `True` для owner/admin/personal-admin/суперадмин
- `get_user_org_role(tid)` → `'owner' | 'admin' | 'user' | None`
- `get_user_org_scope(tid)` → `(scope_type, list[str])` — потолок данных для admin
- `get_db(tid, state)` → правильная DB для пользователя

---

## 2. Регистрация и вход

### Команда `/start`

```
/start
 ├─ Пользователь найден в БД → Главное меню
 └─ Новый пользователь
     ├─ Суперадмин → создать запись в main.db → Главное меню
     └─ Обычный → keyboards.usage_mode_keyboard()
         ├─ "👤 Личное использование"  [mode_personal]
         │    └─ Регистрация (имя, фамилия, телефон, магазин) → shop_bot.db
         ├─ "🏢 Создать организацию"   [mode_corporate]
         │    └─ Создание орга + регистрация owner → main.db + org_N.db
         └─ "🔗 Войти по приглашению" [mode_join]
              └─ Ввод кода приглашения → join org → org_N.db
```

**FSM:** `UserRegistrationStates` (entering_first_name, entering_last_name, entering_phone, entering_shop_name, ...)

**Инвайты:** `generate_invite` → admin_handlers → создаёт одноразовый код в `main.db`

---

## 3. Главное меню

### callback_data: `main_menu`

| Кнопка | callback_data | Кто видит |
|--------|--------------|-----------|
| 💰 ПРОДАЖА | `new_sale` | Все |
| ⚙️ Управление орг. | `admin_management` | `is_any_admin` |
| 🔧 Системная панель | `system_admin_panel` | только суперадмин |
| 📦 ОСТАТКИ | `user_inventory_menu` | только `user` (не-admin) |
| 📝 Мои продажи | `edit_sales_start` | только `user` |
| 📊 Отчеты | `reports` | Все |
| 🏆 Рейтинги | `user_rankings_menu` | Все |
| 📅 Мой график | `my_schedule` | Все |
| 👤 Мой профиль | `user_profile` | Все |
| ℹ Помощь | `help` | Все |

> Admin видит «Управление орг.» вместо «ОСТАТКИ» и «Мои продажи» — эти функции доступны ему через меню управления.

---

## 4. Суперадмин — Системная панель

### callback_data: `system_admin_panel`

```
🔧 Системная панель
 ├─ 💰 Платежная система    [payment_system_admin]  → §10
 ├─ 💾 Резервные копии      [backup_management]
 ├─ 👥 Все пользователи     [admin_users]            → все БД
 ├─ 🏢 Все организации      [list_all_orgs]
 │    ├─ ⚙️ Выбрать орг     [select_org_{id}]        → переключить контекст
 │    └─ 🗑 Удалить орг     [delete_org_{id}]        → confirm_delete_org_{id}
 └─ 🧪 Запустить тесты      [run_system_tests]
      └─ test_imports + test_callbacks + test_scenarios → отчёт
```

---

## 5. Управление организацией

### callback_data: `admin_management`

```
⚙️ Управление орг.
 ├─ 🛍 Упр. товарами        [products]               → §5.1
 ├─ 📦 Упр. остатками       [manage_inventory]        → §5.2
 ├─ 📝 Упр. продажами       [edit_sales]              → §5.3
 ├─ 🎯 Упр. мотивацией      [admin_motivation]        → §5.4
 ├─ 📋 Планы продаж         [admin_sales_plans]       → §5.5
 ├─ 💰 Оклады и смены       [admin_salary_menu]       → §5.6
 ├─ 🏆 Конкурсы             [contests_menu]           → §5.7
 ├─ 👥 Упр. сотрудниками    [admin_users]             → §5.8
 ├─ 🏪 Упр. магазинами      [admin_shops]             → §5.9
 └─ 📊 Google Sheets        [integration_menu]        → §5.10
```

> Суперадмин также видит кнопку смены контекста орга `[change_org_context]`.

---

### 5.1 Товары

**callback_data: `products`**

```
🛍 Управление товарами
 ├─ ➕ Добавить товар        [add_product]
 │    └─ FSM: ProductStates.name → category → price → stock_unit → image(opt) → подтверждение
 ├─ 📥 Добавить списком      [bulk_import_products]
 │    └─ FSM: ввод текстом "Название;Цена;Категория"
 ├─ 📊 Импорт из Excel       [excel_import_products]
 │    └─ Загрузка .xlsx → preview → confirm
 ├─ 📋 Список товаров        [list_products]
 │    ├─ По категориям (счётчик товаров/остатков)
 │    ├─ 🔍 Поиск            [lp_srch_start]
 │    └─ Пагинация           [lp_pg_{n}]
 ├─ 📂 Категории             [categories_menu]
 │    ├─ ➕ Добавить категорию [add_category]
 │    └─ 🗑 Удалить категорию [delete_category]
 ├─ ✏️ Редактировать         [edit_product]
 │    ├─ По категориям → выбор товара [edit_product_{id}]
 │    ├─ 🔍 Поиск            [inv_edit_srch_start] / FSM: SearchStates.inv_edit_srch
 │    └─ Поля: название, цена, категория, единица, изображение
 └─ 🗑 Удалить товар         [delete_product]
      └─ Выбор → подтверждение [confirm_delete_product_{id}]
```

---

### 5.2 Остатки

**callback_data: `manage_inventory` (admin) / `user_inventory_menu` (user)**

```
📦 Управление остатками
 ├─ 👁 Просмотр остатков     [user_inventory_view]
 │    ├─ Список по категориям (🟢/🟡/🔴 по порогу)
 │    ├─ 🔍 Поиск            [inv_view_srch_start]
 │    └─ Пагинация           [inv_view_pg_{n}]
 └─ ✏️ Редактировать         [user_inventory_edit]
      ├─ По категориям (кол-во товаров + сумма остатков)
      │    └─ [edit_inv_category_{name}] → список товаров
      │         └─ [edit_inv_product_{id}]
      │              ├─ ✏️ Изменить остаток  [inv_do_edit_{id}]  → FSM: ввод числа
      │              └─ 📋 История           [inv_log_{id}]       → пагинация логов
      └─ 🔍 Поиск            [inv_edit_srch_start] / FSM: SearchStates.inv_edit_srch
```

---

### 5.3 Продажи — редактирование

**callback_data: `edit_sales` (admin) / `edit_sales_start` (user)**

```
📝 Управление продажами
 ├─ Выбор периода/магазина
 │    ├─ [edit_sales_today]      — сегодня
 │    ├─ [edit_sales_yesterday]  — вчера
 │    ├─ [edit_sales_week]       — неделя
 │    └─ [edit_sales_shop_{n}]   — конкретный магазин (admin)
 ├─ Список продаж с пагинацией  [esl_pg_{n}]
 ├─ 🔍 Поиск по товару          [esl_srch_start] / FSM: SearchStates.edit_sales_srch
 └─ Карточка продажи            [edit_sale_{id}]
      ├─ ✏️ Изм. количество      [es_qty_{id}]
      ├─ ✏️ Изм. цену            [es_price_{id}]
      ├─ ✏️ Изм. дату            [es_date_{id}]
      └─ 🗑 Удалить              [es_del_{id}] → confirm
```

---

### 5.4 Мотивация / Комиссии

**callback_data: `admin_motivation`**

```
🎯 Управление мотивацией
 ├─ ➕ Задать мотивацию       [set_motivation]
 │    ├─ Выбор категории (2-кол. сетка, счётчик товаров) [motiv_cat_{name}]
 │    │    └─ Выбор товара → ввод % или ₽ за единицу / за факт продажи
 │    └─ FSM: CommissionStates.entering_value
 ├─ 👁 Посмотреть все         [view_all_motivations]
 │    └─ Сгруппировано по категориям
 ├─ 🗑 Удалить мотивацию      [remove_motivation]
 │    ├─ Выбор категории      [remove_motiv_cat_{name}]
 │    └─ Выбор товара → подтверждение [rm_motiv_{id}]
 ├─ 📅 Расписание мотиваций   [view_motivation_schedule]
 │    └─ Календарь с отмеченными днями
 └─ ⚙️ Доп. настройки         [motivation_extra]
```

---

### 5.5 Планы продаж

**callback_data: `admin_sales_plans`**

```
📋 Планы продаж
 ├─ ➕ Создать план            [plnwiz_start]
 │    ├─ Цель: Продавец [plntgt_seller] / Магазин [plntgt_shop]
 │    │    ├─ Продавец: список с поиском [plnwiz_srch_user_start]
 │    │    └─ Магазин:  список с поиском [plnwiz_srch_shop_start]
 │    ├─ Период: Неделя [plnper_weekly] / Месяц [plnper_monthly]
 │    ├─ Метрика: Оборот [plnmet_turnover] / Кол-во [plnmet_quantity]
 │    ├─ Фильтр товаров: Все [plnflt_all] / По кат. [plnflt_cat] / По товарам [plnflt_prod]
 │    └─ Ввод целевого значения → подтверждение
 ├─ 📊 Прогресс планов         [plans_progress]
 │    ├─ Прогресс-бары на текущий период
 │    └─ 🔍 Фильтр             [flt_open_plans_progress]
 ├─ ✏️ Редактировать           [editpln_start]
 │    └─ Выбор плана → изменение целевого значения
 └─ 🗑 Удалить план            [delpln_start]
      └─ Выбор плана → подтверждение
```

---

### 5.6 Оклады и смены

**callback_data: `admin_salary_menu`**

```
💰 Оклады и смены
 ├─ 💵 Ставки сотрудников     [slr_rates]
 │    ├─ Список с поиском     [slr_rates_srch_start]
 │    └─ Карточка сотрудника  [slr_set_{uid}]
 │         ├─ Ввод дневной ставки
 │         └─ Тип: оклад / почасовая
 ├─ 📅 Графики работы         [slr_scheds]
 │    ├─ Список с поиском     [slr_scheds_srch_start]
 │    └─ Календарь смен       [slr_cal_{uid}_{y}_{m}]
 │         ├─ День → ✅/🚫 пометить смену  [slr_d_{uid}_{date}] / [slr_rm_{uid}_{date}]
 │         ├─ Время начала                [slr_ets_{uid}_{date}]
 │         ├─ ⏰ Шаблон смен             [slr_tmpl_{uid}_{y}_{m}]
 │         │    └─ Настройка по дням недели (Пн–Вс) с picker часов
 │         └─ Применить шаблон (slr_tog auto-apply)
 ├─ 💼 ФОТ месяца             [slr_sum_{y}_{m}]
 │    └─ Пагинация месяцев    ◀️ / ▶️
 └─ ✏️ Корректировки          [slr_adj_menu]
      ├─ Список сотрудников → [slr_adj_u_{uid}_{y}_{m}]
      └─ ➕ Добавить           [slr_adj_add_{uid}_{y}_{m}]
           └─ Тип: бонус / штраф → сумма → комментарий(opt)

Сотрудник (user):
 └─ 📅 Мой график             [my_schedule]
      └─ Просмотр своего календаря (без редактирования)
```

---

### 5.7 Конкурсы

**callback_data: `contests_menu`**

```
🏆 Конкурсы
 ├─ ➕ Создать конкурс         [contest_create]
 │    ├─ Название → FSM: ContestStates.entering_name
 │    ├─ Описание (опц.)      [contest_skip_desc]
 │    ├─ Фильтр товаров
 │    │    ├─ 📦 Конкретные    [ctscp_product] → поиск/список [ctprd_{id}]
 │    │    ├─ 📂 По категории  [ctscp_category] → поиск/список [ctcat_{name}]
 │    │    └─ 🌐 Все           [ctscp_any]
 │    ├─ Магазины-участники   [ctshopchk_{name}]  (мультивыбор)
 │    ├─ Метрика: Оборот [ctmet_turnover] / Кол-во [ctmet_quantity]
 │    ├─ Тип приза: Итоговый [ctrm_total] / За продажу [ctrm_per_sale]
 │    ├─ Сумма приза → даты начала/конца → подтверждение
 │    └─ APScheduler авто-финиш при достижении даты окончания
 ├─ 🏆 Активные конкурсы      [contest_list_active]
 │    └─ Карточка → прогресс участников → ручной финиш
 └─ 📋 Архив конкурсов        [contest_list_archive]
      ├─ Список завершённых
      └─ 🗑 Очистить архив (с подтверждением)
```

---

### 5.8 Сотрудники

**callback_data: `admin_users`**

```
👥 Управление сотрудниками
 ├─ Список сотрудников (с пагинацией)
 │    ├─ 🔍 Поиск по имени   [adm_usr_srch_start]
 │    ├─ 🔍 Фильтр по scope  [flt_open_admin_users]
 │    └─ Карточка            [admin_user_{tid}]
 │         ├─ ✏️ Редактировать          [admin_edit_user]
 │         ├─ 🎖️ Изменить роль         [adm_role_menu]
 │         │    ├─ 👤 Сотрудник        [adm_role_set_user]
 │         │    ├─ 🛡️ Администратор    [adm_role_set_admin]
 │         │    │    └─ Scope: весь орг / магазин / город / сеть
 │         │    └─ 👑 Директор         [adm_role_set_owner]
 │         ├─ 🏷️ Название должности    [adm_title_start]
 │         ├─ 🚪 Исключить из орга     [adm_kick_confirm]
 │         ├─ 🔄 Восстановить          [adm_restore_confirm]
 │         └─ 🗑 Удалить полностью     [admin_delete_user]
 └─ ⚙️ Управление администраторами [manage_admins]
      ├─ ➕ Добавить                   [add_admin_start]
      └─ ➖ Удалить                    [remove_admin_start]
```

---

### 5.9 Магазины

**callback_data: `admin_shops`**

```
🏪 Управление магазинами
 ├─ Список магазинов (статистика: продажи/сотр./остатки)
 └─ Карточка магазина        [ashop_{name}]
      ├─ 📊 Статистика        [ashop_stats_{name}]
      │    └─ Продажи за сегодня / неделю / месяц
      ├─ 👥 Сотрудники        [ashop_empl_{name}]
      │    └─ Список с ролями
      ├─ 📅 Смены             [ashop_shifts_{name}]
      │    └─ Кто сегодня работает
      ├─ 📦 Остатки           [ashop_inv_{name}]
      │    └─ Топ товаров по остаткам
      ├─ 📝 Заметки           [ashop_notes_{name}]
      │    └─ FSM: AdminShopStates.waiting_for_notes → сохранить
      └─ 🗑 Удалить магазин
           ├─ Перенос данных  [ashop_deltrans_{name}] → [ashop_delto_{to}] → [ashop_delokfin_]
           └─ Без переноса    → подтверждение
```

---

### 5.10 Google Sheets

**callback_data: `integration_menu`** (только Стандарт+ тариф)

```
📊 Google Sheets интеграция
 ├─ Список подключений        [gs_conn_{id}]
 │    ├─ 🔌 Вкл/Выкл          [gs_toggle_conn_{id}]
 │    ├─ 📋 Экспорты           [gs_exports_{id}]
 │    ├─ 📥 Импорт данных      [gs_import_{id}]
 │    ├─ 🔍 Тест подключения   [gs_test_conn_{id}]
 │    ├─ 📊 Синхр. мотивацию  [gs_sync_motiv_{id}]
 │    ├─ 👁 Просмотр мотив.    [gs_show_motiv_{id}]
 │    ├─ 📢 Журнал событий     [gs_log_{id}]
 │    ├─ 🔄 Переавторизация    [gs_reauth_{id}]
 │    ├─ ↔️ Перенести экспорты [gs_move_exports_{id}]
 │    └─ 🗑 Удалить            [gs_del_conn_{id}]
 ├─ ➕ Добавить подключение    [gs_add_conn]
 │    ├─ OAuth (Device Flow)  [gs_auth_oauth]
 │    │    └─ 5-шаговый гайд  [gs_guide_1..5] → [gs_check_secrets]
 │    └─ Service Account      [gs_auth_sa]
 └─ 📖 Инструкция             [gs_guide_1]

Авто-триггер: каждая продажа → trigger_export(db, 'sales', event)
```

---

## 6. Продажа

**callback_data: `new_sale`** (все роли)

```
💰 Новая продажа
 ├─ 🔍 Быстрый поиск         [sale_quick_search]
 │    └─ FSM: SaleStates.searching → case-insensitive поиск в наличии
 ├─ ⭐ Избранное              [sale_show_favorites]
 ├─ 🔄 Недавние              [sale_show_recent]
 ├─ 🏪 Сменить магазин       [sale_change_shop]  (cross-shop sale)
 └─ Категория                [sale_category_{name}]
      └─ Товар               [sale_product_{id}]
           ├─ Кнопки кол-ва: 1,2,3,5,10,20,50  [sq_qty_{n}]
           └─ ✏️ Ввести кол-во вручную          [sq_qty_manual]
                └─ 🛒 Корзина                   [view_cart]
                     ├─ 🗑 Убрать товар         [cart_remove_{id}]
                     ├─ 🗑 Очистить корзину     [clear_cart]
                     ├─ ➕ Добавить ещё         [add_more_items]
                     └─ ✅ Завершить продажу    [complete_sale]
                          ├─ Запись в БД (+ Google Sheets trigger)
                          └─ Push коллегам если shift_sale_alerts=1
```

---

## 7. Отчёты и рейтинги

### callback_data: `reports`

```
📊 Отчёты
 ├─ 📅 За сегодня            [report_today]
 │    └─ Сводка + по магазинам (admin) / свои (user)
 ├─ 📊 Мои продажи           [report_my_shop]    (user/admin с ограниченным scope)
 ├─ 📅 Текущий месяц         [report_my_month]
 ├─ 📅 За месяц (общий)      [report_admin_month]  (admin)
 ├─ 📅 За период             [report_period]
 │    ├─ Выбор дат через календарь
 │    └─ Детализация: Общий [period_report_all] / По магазину [period_report_shop] / По городу [period_report_city]
 ├─ 🏙️ По городу             [report_city] → [city_report_{city}]
 ├─ 📥 Скачать Excel         [download_excel_full / download_excel_user / download_excel_shop_{n}]
 │    └─ Требует подписку Базовый+
 └─ 🔍 Фильтр                [flt_open_reports]
      └─ filter_handlers: shop/city/network (мультивыбор)
```

### callback_data: `rankings_menu` (admin) / `user_rankings_menu` (user)

```
🏆 Рейтинги
 ├─ 👤 Продавцы: 7д [rank_sel_7d] / месяц [rank_sel_month] / прошлый [rank_sel_prev]
 ├─ 🏪 Магазины:  7д [rank_shp_7d] / месяц [rank_shp_month] / прошлый [rank_shp_prev]
 ├─ 🏙️ Города:   7д [rank_cty_7d] / месяц [rank_cty_month] / прошлый [rank_cty_prev]
 ├─ 🔍 Фильтр                [flt_open_rankings_menu]
 └─ 🗑️ Очистить рейтинги     [clear_rankings_confirm] → [clear_rankings_execute]  (admin only)
```

> Позиция пользователя за пределами топ-10 всегда подсвечивается отдельно.

---

## 8. Профиль и настройки

### callback_data: `user_profile`

```
👤 Мой профиль
 ├─ ✏️ Редактировать профиль  [edit_profile]
 │    └─ FSM: UserProfileStates (имя, фамилия, телефон, email, город)
 ├─ 🔔 Уведомления            [notifications_menu]    → §9
 ├─ 🕐 Часовой пояс           [set_timezone_menu]
 │    └─ Список timezone → [set_tz_{zone}]
 └─ 💳 Подписка               [subscription_menu]     → §10
```

---

## 9. Уведомления

### callback_data: `notifications_menu`

```
🔔 Уведомления
 ├─ ⚙️ Настройки             [notification_settings_menu]
 │    ├─ 🔔 Малые остатки     [toggle_notification_setting_low_stock]
 │    ├─ 📊 Ежедневные отчёты [toggle_notification_setting_daily_reports]
 │    ├─ 💰 Новые продажи     [toggle_notification_setting_sales_alerts]
 │    ├─ 👥 Продажи на смене  [toggle_notification_setting_shift_sales]
 │    ├─ 💳 Платёжные         [toggle_notification_setting_payment]
 │    ├─ 📢 От администратора [toggle_notification_setting_admin]
 │    ├─ 📦 Порог остатков    [set_stock_threshold_start] → FSM: ввод числа
 │    └─ ⏰ Время отчёта      [set_notification_time_start] → FSM: ввод ЧЧ:ММ
 ├─ 📋 История                [notification_history_menu]
 │    ├─ Список с пагинацией
 │    ├─ 📅 Фильтр по дате   [nfcal_choosing_start / nfcal_choosing_end]
 │    ├─ ✅ Отметить всё прочитанным [mark_all_notifications_read]
 │    └─ 🗑 Очистить историю [cleanup_notifications_menu] → confirm
 ├─ 📨 Отправить уведомление  [admin_send_notification]  (только admin)
 │    ├─ FSM: waiting_for_admin_message → ввод текста
 │    └─ Выбор получателей:
 │         ├─ 👥 Всем                    [ntf_rcpt_all]
 │         ├─ 🏪 По магазину             [ntf_rcpt_shop] → [ntf_rcpt_s_{name}]
 │         ├─ 🎭 По роли                 [ntf_rcpt_role] → [ntf_rcpt_r_{role}]
 │         ├─ 🏙 По городу               [ntf_rcpt_city] → [ntf_rcpt_ci_{city}]
 │         ├─ 🌐 По торговой сети        [ntf_rcpt_network] → [ntf_rcpt_nw_{net}]
 │         └─ 👤 Выбрать конкретных      [ntf_rcpt_pick]
 │              ├─ Чекбоксы по 8/стр.   [ntf_usr_tog_{tid}]
 │              ├─ Выбрать стр.          [ntf_usr_all_{pg}] / [ntf_usr_none_{pg}]
 │              ├─ Пагинация             [ntf_usr_pg_{n}]
 │              └─ ✅ Готово             [ntf_usr_done]
 │         Превью → [admin_confirm_send_now] или [admin_schedule_notification]
 │              └─ FSM: waiting_for_schedule_time → ввод ДД.ММ.ГГГГ ЧЧ:ММ
 └─ 📅 Запланированные        [view_scheduled_notifications]
      └─ [delete_scheduled_notification_{id}]
```

**APScheduler-джобы:**
- `send_scheduled_notifications` — каждую минуту, пул всех БД
- `send_payment_alerts` — за 14/7/3/1 день до конца подписки
- `send_trial_expired_upsell` — каждый час :05 при истечении триала
- `low_stock_alerts` — ежедневно
- `daily_sales_report` — ежедневно
- `shift_sale_alerts` — при каждой продаже (коллегам на смене)
- `auto_reject_stale_payments` — ежедневно 10:15 (СБП > 72 ч → отклонить)

---

## 10. Подписки и оплата

### callback_data: `subscription_menu`

```
💳 Подписка
 ├─ 📊 Мой статус / Мои лимиты  [subscription_limits]
 │    └─ Текущий тариф, лимиты (товары / магазины / продажи)
 └─ 💳 Купить подписку          [subscription_plans]
      ├─ Тарифная сетка:
      │    Бесплатный (0₽) / Базовый (500₽/30д) / Стандарт (1200₽/90д) / Премиум (4000₽/365д)
      ├─ При выборе плана → [plan_{key}]
      │    ├─ ⏰ Отложенная активация  [schedule_{key}]
      │    └─ ⚡ Немедленная замена    [immediate_{key}]
      ├─ 🎁 Промокод                   [enter_promocode_{key}]
      └─ 💳 К оплате                  [proceed_payment_{key}]
           ├─ СБП (скриншот)
           │    └─ Загрузка фото → создание payment_request → ожидание подтверждения admin
           └─ YooKassa (автоматически)
                └─ Платёжная ссылка → [yk_check_{id}] проверка статуса
```

**Тарифные ограничения:**

| Тариф | Товары | Магазины | Продаж/мес | Экспорт | Аналитика | Интеграции |
|-------|--------|----------|------------|---------|-----------|------------|
| Бесплатный | 50 | 1 | 100 | ❌ | ❌ | ❌ |
| Базовый | 200 | 3 | 500 | ✅ | ✅ | ❌ |
| Стандарт | 500 | 10 | 1500 | ✅ | ✅ | ✅ GSheets |
| Премиум | ∞ | ∞ | ∞ | ✅ | ✅ | ✅ |
| Trial 14д | ∞ | ∞ | ∞ | ✅ | ✅ | ✅ |

**Суперадмин — Платёжная система** (`payment_system_admin`):

```
💰 Платёжная система
 ├─ 💳 Заявки на оплату       [pending_payments]
 │    └─ [view_payment_{id}] → [confirm_payment_{id}] / [reject_payment_{id}]
 ├─ ⚙️ Настройки оплаты       [payment_settings]
 │    ├─ Провайдер: СБП / YooKassa  [payment_provider_select]
 │    ├─ Номер карты           [set_card_number]
 │    ├─ Получатель            [set_recipient_name]
 │    ├─ Банк                  [set_bank_name]
 │    └─ Инструкция            [set_payment_instruction]
 ├─ 💎 Управление тарифами     [manage_plans]
 │    ├─ ➕ Добавить           [add_plan]
 │    ├─ ✏️ Редактировать      [edit_plan] → [edit_plan_{id}]
 │    ├─ 🔄 Вкл/Выкл          [toggle_plan]
 │    ├─ 🗑 Удалить            [delete_plan]
 │    └─ 🎯 Скидки            [set_discounts]
 ├─ 📊 Статистика платежей     [payment_statistics]
 │    └─ За период [stats_by_period] / Экспорт [export_payment_report] / Графики [payment_charts]
 ├─ 👥 Управление подписками   [manage_subscriptions]
 │    ├─ 🔍 Найти              [find_user_subscription]
 │    ├─ 🎁 Выдать             [grant_subscription]
 │    ├─ ⏰ Продлить           [extend_subscription]
 │    ├─ ❌ Отменить           [cancel_subscription]
 │    └─ 📊 Детали             [subscription_details]
 ├─ 🎁 Промокоды               [manage_promocodes]
 │    ├─ ➕ Создать            [create_promocode]
 │    ├─ ✏️ Редактировать      [edit_promocode]
 │    ├─ 📊 Статистика         [promocode_stats]
 │    └─ 🗑 Удалить            [delete_promocode]
 └─ 🎫 Пробный период          [trial_settings]
```

---

## 11. Личный кабинет

Режим личного использования (`data/shop_bot.db`, `ADMIN_CHAT_ID`).

Тот же интерфейс, что у **owner** организации, но:
- Нет многопользовательского режима (только один пользователь)
- Нет системы ролей / инвайтов
- Нет фильтрации по scope
- Доступ к «Управление орг.» через `is_any_admin` → `True`
- Суперадмин видит дополнительно «🔧 Системная панель»

**Переключение контекста** (суперадмин + орг-пользователь):
- [change_org_context] — показывает список организаций
- [select_org_0] — переключиться на личную БД
- [select_org_{id}] — переключиться на org_N.db

---

## 12. FSM-состояния

| Класс | Состояния |
|-------|-----------|
| `ProductStates` | name, category, price, stock_unit, image, confirm |
| `InventoryStates` | editing |
| `SaleStates` | searching, entering_quantity |
| `EditSaleStates` | choosing_sale, editing_quantity, editing_price, editing_date |
| `SearchStates` | inv_edit_srch, edit_sales_srch |
| `ReportStates` | choosing_start, choosing_end |
| `UserRegistrationStates` | mode, first_name, last_name, phone, shop_name, city, org_name, invite_code |
| `AdminUserStates` | editing_user, entering_scope |
| `UserProfileStates` | editing_name, editing_phone, editing_city |
| `AdminManagementStates` | bulk_import, excel_import |
| `NotificationStates` | waiting_for_threshold, waiting_for_time, waiting_for_admin_message, waiting_for_schedule_time, nfcal_choosing_start, nfcal_choosing_end |
| `CommissionStates` | entering_value |
| `ContestStates` | entering_name, entering_description, selecting_scope, selecting_products, selecting_shops, entering_reward, entering_start_date, entering_end_date |
| `SalesPlanStates` | entering_target |
| `AdminShopStates` | waiting_for_city, waiting_for_network, waiting_for_notes |
| `SalaryStates` | entering_rate, entering_adjustment |
| `PaymentSystemStates` | waiting_card_number, waiting_recipient_name, waiting_bank_name, waiting_plan_name, waiting_plan_price, ... |
| `IntegrationStates` | waiting_spreadsheet_id, waiting_sheet_name, ... |

---

## 13. Быстрый справочник callback_data

### Корневые точки входа

| callback_data | Раздел |
|--------------|--------|
| `main_menu` | Главное меню |
| `new_sale` | Начать продажу |
| `admin_management` | Управление (owner/admin) |
| `system_admin_panel` | Системная панель (суперадмин) |
| `reports` | Отчёты |
| `user_rankings_menu` | Рейтинги |
| `my_schedule` | Мой график |
| `user_profile` | Профиль |
| `notifications_menu` | Уведомления |
| `subscription_menu` | Подписка |

### Паттерны callback_data с переменными

| Паттерн | Описание |
|---------|----------|
| `edit_sale_{id}` | Карточка продажи для редактирования |
| `admin_user_{tid}` | Карточка сотрудника |
| `ashop_{name}` | Карточка магазина |
| `gs_conn_{id}` | Google Sheets подключение |
| `slr_cal_{uid}_{y}_{m}` | Календарь смен сотрудника |
| `slr_sum_{y}_{m}` | ФОТ за месяц |
| `plnshp_{name}` / `plnusr_{uid}` | Цель плана |
| `ntf_rcpt_s_{name}` | Магазин для рассылки |
| `ntf_usr_tog_{tid}` | Переключить конкретного получателя |
| `rank_sel_7d/month/prev` | Период рейтинга продавцов |
| `flt_open_{back_cb}` | Открыть панель фильтров |
| `ftog_s/c/n_{val}` | Переключить магазин/город/сеть в фильтре |
| `safe_cb(val, prefix)` | Хэш длинных строк >64 байт |

### Фильтры (filter_handlers)

```
flt_open_{back_cb}     — открыть панель (back_cb = куда вернуться)
ftog_s_{shop}          — тоггл магазина
ftog_c_{city}          — тоггл города
ftog_n_{network}       — тоггл торговой сети
flt_reset              — сбросить фильтр
```

Состояние фильтра хранится в FSM key `admin_filter`, сохраняется `clear_state_keep_org()`.

---

## Архитектурные правила для агентов

1. **`clear_state_keep_org(state)` — только ПОСЛЕ `fsm_edit()`**, иначе state очистится до редактирования
2. **`he(user_string)`** — обязателен для всех пользовательских строк в HTML-сообщениях
3. **`safe_cb(val, prefix)` / `resolve_cb_name()`** — для любых пользовательских строк в callback_data (лимит 64 байта)
4. **`get_db(tid, state)`** — всегда через этот метод, никогда напрямую `Database(...)`
5. **Новый роутер** → зарегистрировать в `main.py`; новый модуль → добавить в `test_imports.py`
6. **`state.clear()` запрещён** — только `clear_state_keep_org(state)`
7. **`logger`** не объявлен глобально в `sales_handlers.py` — использовать `logging.error()` напрямую
8. **`get_sales_ranking()`** → 8 колонок; 8-я = `u.id`; для 7-колоночного распаковывания использовать `row[:7]`
9. **`Database.db_file`** (не `.db_path`) — атрибут пути к файлу БД
10. **`datetime.now()` на Amvera = UTC** — для отображения использовать `timezone_utils`; для сохранения ввода пользователя — `get_utc_time()`
