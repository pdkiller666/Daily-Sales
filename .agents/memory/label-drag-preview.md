---
name: Label drag-on-preview
description: Перетаскивание блоков ценника прямо на визуальном макете (label.html), синхронизация с боковой панелью
---

# Drag-по-макету в конструкторе ценников

Блоки ценника (`.lbl-block[data-block]`) можно перетаскивать прямо на превью, не только в боковой панели.

**Архитектура (один источник правды):**
- `elemOrder` (Alpine state) — единственный источник порядка. И панель (`x-for="key in elemOrder"`), и drag-по-макету мутируют его → двусторонняя синхронизация бесплатно.
- `initPreviewSortable()` вешает SortableJS на ПЕРВЫЙ `.label` в `#label-wrap`; `onEnd` читает DOM-порядок → `elemOrder` → `applyOrder()` распространяет на все копии.
- Сохранение: `saveDesign()` шлёт `element_order = JSON.stringify(elemOrder)` — тот же путь что и панель. БД/бэкенд не трогались (колонка `element_order` уже была).

**Why:** ценник — линейный вертикальный список, поэтому drag = вертикальная сортировка, НЕ свободный холст (свободный холст был осознанно отклонён как избыточный — потребовал бы absolute-координат + пересчёта в PDF).

**How to apply / грабли:**
- `_previewSortables[]` — массив инстансов; destroy+recreate в `renderCopies()` обязателен, иначе утечка инстансов на каждое изменение копий.
- `filter:'.hidden-elem'` — скрытые блоки не перетаскиваются.
- Мобайл: `delay:150, delayOnTouchOnly:true, touchStartThreshold:5` — иначе тап-скролл путается с drag.
- ⚠️ Любые новые UI-подсказки/drag-affordance ОБЯЗАНЫ скрываться в `@media print` (`.preview-edit-hint`, cursor/background/box-shadow на `.label.editable .lbl-block`) — иначе печатаются на ценнике. Был замечен code-review.
