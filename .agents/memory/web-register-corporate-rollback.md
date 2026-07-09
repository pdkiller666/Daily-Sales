---
name: Corporate registration orphaned credential rollback
description: Why register_submit must delete the web_credential when create_organization fails in corporate mode
---

В `register_submit` (`web/routes/email_auth.py`) для `usage_mode="corporate"` порядок операций такой: сначала создаётся `web_credential` (email занимается по UNIQUE), затем вызывается `tenant_manager.create_organization()`. Если создание организации падает (дублирующееся имя, exception), credential уже существует — email оказывается "сожжён" навсегда, а пользователь не может повторно зарегистрироваться.

**Почему это важно:** без явного роллбэка пользователь получает "email уже зарегистрирован" при повторной попытке, хотя организация не создалась. При следующем логине аккаунт тихо деградирует до personal-режима (роль эскалируется до admin, т.к. org_db отсутствует) — не потеря данных, но путаная UX, а в `/settings` нет пути создать организацию постфактум для такого аккаунта.

**Как применять:** при любом провале `create_organization()` в этой ветке — сразу вызывать `db.delete_web_credential_by_id(cred_id)` до возврата ошибки. Уже исправлено в коде. Если добавляется новый шаг между созданием credential и финализацией (например, join-режим с инвайтом), применять тот же паттерн откат-при-ошибке.
