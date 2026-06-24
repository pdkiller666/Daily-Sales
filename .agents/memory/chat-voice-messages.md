---
name: Chat voice messages player
description: Кастомный <voice-msg> плеер заменил нативный <audio> в чате; чинит webm Infinity-duration; почему color:inherit обязателен на кнопке
---

# Голосовые сообщения в чате

Запись (MediaRecorder, webm/ogg/mp4, лимит 5 мин), загрузка через `/chat/send` и
`/chat/dm/send` (`is_audio` флаг), WS-доставка уже были в коде. Доводка свелась к ПЛЕЕРУ.

## Что заменено
Нативный `<audio controls>` (класс `.voice-player`, был ужасен + баг длительности)
→ кастомный Web Component `<voice-msg src="...">` в `web/templates/chat/index.html`
(CSS `.vmsg*` рядом со старым `.voice-player`; определение — отдельный `<script>`
ПЕРЕД блоком `function chatApp(...)`). Рендерится в обеих лентах (группа + ЛС),
для входящих и исходящих — всего 2 `<template x-if="f.is_audio">` покрывают все 4 случая.

## Гочи (durable)

- **webm/opus от MediaRecorder не имеет длины в метаданных** → `audio.duration === Infinity`,
  нативная перемотка мертва. Фикс: на `loadedmetadata` при Infinity/NaN ставим
  `audio.currentTime = 1e101` (форс-seek в конец) → браузер досчитывает реальную длину,
  ловим её в одноразовом `timeupdate`, возвращаем `currentTime = 0`. Классический приём.
- **`<button>` НЕ наследует `color` по умолчанию** → `background:currentColor` на кнопке
  резолвится в чёрный, а НЕ в цвет текста пузыря. Обязателен явный `color:inherit` на
  `.vmsg-btn`, иначе «мой» (синий) пузырь получает чёрный кружок вместо белого.
  **Why:** двухтоновый Telegram-вид (тёмный кружок/белая иконка во входящих, белый
  кружок/синяя иконка в исходящих) держится на наследовании цвета текста пузыря через
  `currentColor`; кнопка рвёт цепочку наследования.
- CSP уже разрешает inline-script (`'unsafe-inline'`) и same-origin media (`default-src 'self'`,
  аудио = `/chat/file/attachment/{id}`), `microphone=(self)` уже стоит — менять не нужно.

## Тест плеера без Telegram-логина
Логина в чат через Telegram-код в Replit нет. Проверял генерацией same-origin WAV в
`web/static/` + временной тест-страницей `/static/_vmsg_test.html` (CSP media-src='self'
блокирует `data:`/внешние URL — нужен файл на своём origin). Статика кэшируется — для
свежего скриншота копировать под новым именем. Тестовые файлы удалять после.
