---
name: Huawei Web Push background limitation
description: Why Web Push doesn't wake browsers in background on Huawei/GMS-less devices — device limitation, not server bug
---

Фоновая доставка Web Push в Chromium-браузерах опирается на Google FCM. На устройствах без Google Play Services (GMS) — типично Huawei после 2019 — браузер НЕ будит фоновый процесс: пуши копятся и приходят пачкой при открытии PWA.

**Why:** это ограничение устройства/ОС, не сервера. Сервер (pywebpush/VAPID), подписка и SW корректны — диагностировано по реальному инциденту с Huawei.

**How to apply:**
- Не «чинить» это на сервере. Для таких клиентов уведомления всё равно дублируются в Telegram и в колокольчик кабинета.
- Эндпоинт подписки FCM (`fcm.googleapis.com`/`android.googleapis.com`) на GMS-less устройстве = риск фоновой недоставки; супер-админ страница `/admin/push-diagnostics` помечает такие подписки.
- Реальное решение фоновых пушей на Huawei — нативное/TWA приложение с HMS Push Kit (отдельный план).
