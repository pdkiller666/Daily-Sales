---
name: Веб-регистрация с 3 режимами (personal/corporate/join)
description: /register реализует те же режимы, что и бот (личное/организация/приглашение); нюансы rate-limit при тестировании и роли synthetic-аккаунтов
---

`web/routes/email_auth.py::register_submit` ветвится по `usage_mode` (personal/corporate/join), повторяя логику `process_city` из `handlers.py`: personal и corporate создают профиль + триал-подписку (`create_trial_subscription`), corporate дополнительно вызывает `tenant_manager.create_organization()`; join ищет org по инвайт-коду.

Для synthetic web-аккаунтов (`tg_id<0`) роль в `_issue_session_response` повышается до `admin`, если нет записи в `user_org_mapping` (значит personal-режим без организации) — иначе такие пользователи логинились бы с ролью `user` без доступа к своим же данным.

**Rate-limit при ручном тестировании через curl**: `web/rate_store.py` (`rate_hits` в `data/rate_limits.db`) создаёт таблицу лениво внутри `_ensure_tables()`, вызываемой только из `check_rate_limit()`. Прямой `sqlite3.connect(...).execute("DELETE FROM rate_hits...")` в отдельном скрипте падает с `no such table`, если таблицу ещё не создавал сам процесс. Чтобы сбросить лимит для тестов: сначала `from web.rate_store import check_rate_limit; check_rate_limit('warmup', 100, 1)` (создаёт схему), потом уже `DELETE FROM rate_hits WHERE key LIKE 'register:%'`.
