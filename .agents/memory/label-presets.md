---
name: Label design presets architecture
description: Why ценник-пресеты — отдельная библиотека поверх активного singleton, и почему logo_path мёржится на бэкенде
---

# Пресеты дизайна ценников

Рендеринг ценников (PDF/экран) всегда читает **активную** одиночную настройку
`org_label_settings` id=1 через `get_label_settings()`. Пресеты — отдельная
библиотека оформлений (таблица `org_label_presets`, много строк per-org).

- «Применить пресет» = копировать его поля в активный singleton через
  `save_label_settings()`. Рендеринг НЕ трогали → обратная совместимость.
- «Сохранить как пресет» = новая строка с текущим дизайном. Лимит 30.

**Why (logo_path мёрж на бэкенде):** фронтовый `_designFields()` НЕ шлёт
`logo_path` (это серверный путь загруженного файла, не редактируемое поле формы).
Если просто сохранить форму — пресет получит пустой logo, и применение затрёт
активный логотип. Поэтому при создании пресета бэкенд, если форма не прислала
logo_path, подставляет текущий активный `get_label_settings()["logo_path"]` →
roundtrip lossless.

**How to apply:** любое новое поле дизайна, которого нет в форме (как logo_path),
обязано мёржиться из активных настроек при create/update пресета, иначе apply его
обнулит. JSON-поля (`element_order`/`visible_elements`) `get_label_settings()`
возвращает уже распарсенными в объекты, а пресет хранит сырые JSON-строки — при
сравнении/тестах это учитывать. Регресс: `test_label_presets.py`.

## Новые поля пресета: ALTER только ПОСЛЕ CREATE TABLE
Добавляя колонку в `org_label_presets` (напр. `qr_content`): её ALTER-бэкфилл
для legacy-БД обязан идти ПОСЛЕ `CREATE TABLE org_label_presets`, не в общем
списке `_label_alters` (тот выполняется раньше — на свежей БД таблицы ещё нет →
ALTER молча/громко мимо). Паттерн: CREATE TABLE содержит новую колонку сразу,
плюс отдельный guarded ALTER ниже для уже существующих таблиц.
**Why:** на чистой установке ALTER-до-CREATE падает/no-op; на legacy без бэкфилла
колонки нет → save/get падают. Нужны ОБА: колонка в CREATE + ALTER после него.

## Предпросмотр пресета (Фаза 7)
GET `/products/label-presets/{id}` (guard `_label_presets_guard`, synchronous,
без CSRF/form — GET) отдаёт полный design JSON. Фронт `previewPreset()`: один раз
бэкапит текущий `this.d/visElems/elemOrder` (`_snapshotDesign`), применяет дизайн
пресета к ЖИВОМУ превью БЕЗ сохранения; `cancelPreview()` восстанавливает бэкап;
`applyPreviewed()` зовёт существующий POST `/apply` (save+reload). При delete
превьюшного пресета — `cancelPreview()` чтобы не висел stale-баннер.
`_presetToState` реверс-маппит CSS-шрифт→key (Georgia→serif и т.д.), парсит
JSON-строки element_order/visible_elements. QR live-превью НЕ обновляется при
вводе qr_content (qr_b64 рендерит сервер) — допустимое ограничение.
