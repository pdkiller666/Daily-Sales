# APK Delivery System — полная инструкция

Эта инструкция описывает всю цепочку: GitHub Actions собирает APK → отправляет его на Amvera → сервер отдаёт файл пользователям через `/download/android`. Все детали и тонкости описаны для воспроизведения аналогичной системы.

---

## Архитектура (общая схема)

```
Push в GitHub (android/ или .github/workflows/)
    → GitHub Actions: сборка Gradle → подписанный APK
    → GitHub Releases: создание тега apk-NNN, загрузка файла
    → POST /webhook/apk-binary  — бинарь APK напрямую на Amvera (основной путь)
    → POST /webhook/apk-release — метаданные (версия, URL, дата) в БД
    → Amvera: APK сохранён в data/apk/DailySales-latest.apk (persistent volume)

Пользователь открывает /download/android
    → файл уже есть → FileResponse прямо с диска
    → файла нет → фоновая загрузка из GitHub + страница ожидания с авторефрешем

При старте сервера (on_event startup)
    → если data/apk/ пуст → запрос к GitHub Releases API → скачать последний APK
```

---

## 1. GitHub Actions — файл `.github/workflows/build-twa.yml`

### Триггер
```yaml
on:
  push:
    branches: [main, master]
    paths:
      - 'android/**'
      - 'twa-manifest.json'
      - '.github/workflows/build-twa.yml'
  workflow_dispatch:
```
Сборка идёт только когда меняется Android-часть — не на каждый пуш.

### Шаги сборки

**1. Java 17 + Android SDK + Gradle 8.4**
```yaml
- uses: actions/setup-java@v4
  with:
    java-version: '17'
    distribution: 'temurin'
- uses: android-actions/setup-android@v3
- run: sdkmanager "platforms;android-34" "build-tools;34.0.0"
- uses: gradle/actions/setup-gradle@v3
  with:
    gradle-version: '8.4'
```

**Критический gotcha — bubblewrap не работает в CI:**
`bubblewrap` CLI (официальный инструмент Google для TWA) даёт константную ошибку "androidSdk isn't correct" в GitHub Actions. Решение — прямой Gradle build из папки `android/`. В `android/gradle.properties` обязательна строка `android.useAndroidX=true`.

**2. Keystore**
```yaml
- run: echo "${{ secrets.KEYSTORE_BASE64 }}" | base64 -d > "${{ github.workspace }}/dailysales-release.p12"
```
Проверяет размер — если < 100 байт, считает KEYSTORE_BASE64 невалидным и падает.

**3. Gradle build**
```yaml
- run: |
    gradle :app:assembleRelease \
      --project-dir android --no-daemon --info > build.log 2>&1
  env:
    KEYSTORE_PASSWORD: ${{ secrets.KEYSTORE_PASSWORD }}
    KEYSTORE_PATH: ${{ github.workspace }}/dailysales-release.p12
```

**Gotcha — secrets нельзя в `if:` условии:**
`if: secrets.FOO != ''` — синтаксическая ошибка GitHub Actions. Проверяй секрет через env var в shell: `[ -n "$MY_SECRET" ] || exit 0`.

**4. Публикация в GitHub Releases**
```yaml
- uses: softprops/action-gh-release@v2
  with:
    tag_name: apk-${{ github.run_number }}
    name: "DailySales APK — сборка #${{ github.run_number }}"
    files: ${{ env.APK_FILE }}
```

**5. Отправка бинаря напрямую на Amvera** (основной путь)
```bash
STATUS=$(curl -s -o /tmp/apk_upload_resp.txt -w "%{http_code}" \
  --retry 12 --retry-delay 20 --retry-max-time 300 \
  --retry-all-errors --retry-connrefused \
  -X POST "https://YOUR_APP.amvera.io/webhook/apk-binary" \
  -H "Authorization: Bearer ${APK_WEBHOOK_SECRET}" \
  -H "X-APK-Version: ${VERSION}" \
  -H "Content-Type: application/octet-stream" \
  --data-binary "@${APK_FILE}" --max-time 120 || echo "000")
[ "${STATUS}" = "200" ] || exit 1
```

**Почему --retry 12 с задержкой 20 с?**
Тот же пуш одновременно деплоит код на Amvera. Amvera перестартует сервер, и в это время `/webhook/apk-binary` даёт 503/connrefused. 12 попыток × 20 с = до 4 минут ожидания — хватает для рестарта.

**6. Уведомление сервера о метаданных** (вторичный путь)
```bash
PAYLOAD="{\"version\":\"v1.0.${RUN}\",\"release_url\":\"...\",
           \"apk_url\":\"https://github.com/.../releases/download/apk-${RUN}/File.apk\",
           \"release_date\":\"...\",\"native\":true}"
curl -X POST "https://YOUR_APP.amvera.io/webhook/apk-release" \
     -H "Authorization: Bearer $APK_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d "$PAYLOAD"
```
Поле `"native": true` говорит серверу отправить уведомления в Telegram.

**GitHub Secrets (обязательные):**
- `KEYSTORE_BASE64` — p12-файл в base64
- `KEYSTORE_PASSWORD` — пароль к keystore
- `APK_WEBHOOK_SECRET` — токен для webhook (такой же в Replit Secrets)

---

## 2. Сервер — `web/app.py`

### Хранилище

```python
_APK_LOCAL   = Path("data/apk/DailySales-latest.apk")   # persistent volume Amvera
_APK_MIN_SIZE = 200_000   # ~200 KB — защита от повреждённых/пустых файлов
```

`data/` смонтирован как `persistenceMount: /app/data` в `amvera.yml`. Файл переживает рестарты и редеплои.

### `POST /webhook/apk-binary` — приём бинаря

Принимает APK-файл напрямую от GitHub Actions. **Это основной путь** — не требует токена GitHub на сервере и работает с приватным репозиторием.

```python
@app.post("/webhook/apk-binary", include_in_schema=False)
async def webhook_apk_binary(request: Request):
    # 1. Аутентификация — Bearer токен (hmac.compare_digest для constant-time)
    secret = os.environ.get("APK_WEBHOOK_SECRET", "")
    token  = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # 2. Защита от гигантских файлов (50 MB hard cap)
    version = request.headers.get("X-APK-Version", "").strip()
    _APK_MAX_SIZE = 50 * 1024 * 1024
    body = await request.body()
    if len(body) > _APK_MAX_SIZE:
        return JSONResponse({"error": "file too large"}, status_code=413)
    if len(body) < _APK_MIN_SIZE:
        return JSONResponse({"error": f"file too small ({len(body)} bytes)"}, status_code=400)

    # 3. Атомарная запись: сначала .tmp, потом rename (чтобы не отдать битый файл)
    _APK_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = _APK_LOCAL.with_suffix(".apk.tmp")
    tmp.write_bytes(body)
    tmp.replace(_APK_LOCAL)   # атомарный rename

    return JSONResponse({"ok": True, "bytes": len(body), "version": version})
```

### `POST /webhook/apk-release` — приём метаданных

Сохраняет версию, URL, дату в `payment_settings` (таблица в `shop_bot.db`) и опционально запускает фоновую загрузку APK с GitHub.

```python
@app.post("/webhook/apk-release", include_in_schema=False)
async def webhook_apk_release(request: Request, background_tasks: BackgroundTasks):
    # ... auth аналогично apk-binary ...
    body = await request.json()
    version      = body.get("version", "")
    release_url  = body.get("release_url", "")
    apk_url      = body.get("apk_url", "")
    release_date = body.get("release_date", "")
    native       = body.get("native", False)   # True → отправить Telegram-уведомления

    # Сохранить метаданные в payment_settings
    conn = sqlite3.connect(_SHOP_BOT_DB)
    conn.execute("INSERT OR REPLACE INTO payment_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                 ("apk_latest_version", version))
    conn.execute("INSERT OR REPLACE INTO payment_settings (...)", ("apk_download_url", apk_url))
    conn.execute("INSERT OR REPLACE INTO payment_settings (...)", ("apk_release_url", release_url))
    conn.execute("INSERT OR REPLACE INTO payment_settings (...)", ("apk_release_date", release_date))
    conn.execute("INSERT INTO apk_release_history (version, release_url, release_date) VALUES (?, ?, ?)",
                 (version, release_url, release_date))
    conn.commit()

    # Если native=True — Telegram-уведомления admin + владельцам (у кого включены)
    if native:
        # ... отправить боту через asyncio.run_coroutine_threadsafe в _main_loop ...

    # Фоновая скачка (запасной путь если apk-binary не пришёл)
    if apk_url:
        background_tasks.add_task(_download_apk_to_local, apk_url)

    return JSONResponse({"ok": True, "version": version})
```

**Таблицы БД (`shop_bot.db`):**
- `payment_settings` — ключи `apk_latest_version`, `apk_download_url`, `apk_release_url`, `apk_release_date`
- `apk_release_history (version, release_url, release_date)` — история релизов
- `data/main.db`: `apk_notif_prefs (telegram_id, enabled)` — настройки уведомлений по владельцам

### `GET /download/android` — отдача APK пользователю

```python
@app.get("/download/android", include_in_schema=False)
async def download_android(request: Request):
    # Логируем скачивание (referrer, user_agent, telegram_id) — в download_events
    ...

    # Путь 1: файл уже есть → отдаём сразу
    if _APK_LOCAL.exists() and _APK_LOCAL.stat().st_size >= _APK_MIN_SIZE:
        return FileResponse(
            str(_APK_LOCAL),
            media_type="application/vnd.android.package-archive",
            filename="DailySales.apk",
        )

    # Путь 2: файла нет → запускаем фоновую загрузку и показываем страницу ожидания
    apk_url = db_read("apk_download_url from payment_settings")
    if apk_url:
        asyncio.ensure_future(_download_apk_to_local(apk_url))

    return HTMLResponse("""
        <meta http-equiv="refresh" content="6;url=/download/android">
        ... страница "APK загружается, обновится через 6 секунд" ...
    """)
```

### `_download_apk_to_local` — асинхронная загрузка с GitHub

**Критический gotcha для приватного репозитория:**
`browser_download_url` (github.com/.../releases/download/TAG/NAME) даёт HTTP 404 даже с Bearer-токеном. Нужно:
1. Резолвить через GitHub API: `GET /repos/OWNER/REPO/releases/tags/TAG` → найти asset по имени → взять `asset["url"]` (это API-URL вида `api.github.com/.../assets/ID`)
2. Скачивать API-URL с заголовком `Accept: application/octet-stream` + Bearer

```python
async def _download_apk_to_local(apk_url: str) -> None:
    tmp = _APK_LOCAL.with_suffix(".tmp")
    _gh_token = os.environ.get("GITHUB_TOKEN", "")

    async with aiohttp.ClientSession() as session:
        download_url = apk_url

        # Только если browser_download_url (не API-URL)
        if _gh_token and "github.com" in apk_url and "/releases/assets/" not in apk_url:
            m = re.search(
                r"github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$", apk_url
            )
            if m:
                owner, repo, tag, name = m.groups()
                api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
                async with session.get(api_url, headers={
                    "Authorization": f"Bearer {_gh_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }) as r:
                    if r.status == 200:
                        rel = await r.json()
                        for a in rel.get("assets", []):
                            if a.get("name") == name:
                                download_url = a.get("url") or download_url
                                break

        # Скачиваем
        headers = {"User-Agent": "MyApp/1.0"}
        if _gh_token and "github.com" in download_url:
            headers["Authorization"] = f"Bearer {_gh_token}"
            if "/releases/assets/" in download_url:
                headers["Accept"] = "application/octet-stream"   # ОБЯЗАТЕЛЬНО для API-URL

        async with session.get(download_url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=180),
                               allow_redirects=True) as resp:
            if resp.status != 200:
                return
            with open(tmp, "wb") as f:
                async for chunk in resp.content.iter_chunked(65536):
                    f.write(chunk)

    # Атомарная замена только если файл валиден
    if tmp.exists() and tmp.stat().st_size >= _APK_MIN_SIZE:
        tmp.replace(_APK_LOCAL)
    else:
        tmp.unlink(missing_ok=True)
```

### `on_event("startup")` — первый старт / рестарт без APK

```python
@app.on_event("startup")
async def _startup_ensure_apk():
    if _APK_LOCAL.exists() and _APK_LOCAL.stat().st_size >= _APK_MIN_SIZE:
        _log.info(f"APK already cached: {_APK_LOCAL}")
        return

    # Запросить последний релиз с GitHub
    api_url = "https://api.github.com/repos/OWNER/REPO/releases/latest"
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers=gh_headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            release = await resp.json()

    # Найти .apk ассет
    apk_asset = next((a for a in release.get("assets", [])
                      if a["name"].endswith(".apk")), None)
    if apk_asset:
        await _download_apk_to_local(apk_asset["browser_download_url"])
        # Сохранить в БД
        db_write("apk_latest_version", release["tag_name"])
        db_write("apk_download_url", apk_asset["browser_download_url"])
```

Этот код срабатывает при каждом старте приложения. Если `data/apk/DailySales-latest.apk` уже есть — ничего не делает (быстрый путь).

---

## 3. Бот — `handlers.py`

### Callback `apk_info`

```python
@router.callback_query(F.data == "apk_info")
async def apk_info_handler(callback, state):
    notif_on = _apk_notif_get(callback.from_user.id)
    builder.add(InlineKeyboardButton(
        text="⬇️ Скачать APK",
        url="https://YOUR_APP.amvera.io/download/android"
    ))
    builder.add(InlineKeyboardButton(
        text="🔔 Уведомления: ✅" if notif_on else "🔕 Уведомления: ❌",
        callback_data="toggle_apk_notif"
    ))
```

### Таблица уведомлений `apk_notif_prefs`

```sql
CREATE TABLE IF NOT EXISTS apk_notif_prefs (
    telegram_id INTEGER PRIMARY KEY,
    enabled     INTEGER DEFAULT 0
)
```

Хранится в `data/main.db`. Настройка «Получать уведомления об APK» — сохраняется через `_apk_notif_set(tg_id, enabled)` и проверяется вебхуком перед рассылкой.

---

## 4. Веб-настройки — `web/routes/admin.py` и `web/routes/settings.py`

- `GET /admin/apk` — суперадмин-страница с историей релизов (из `apk_release_history`) и кнопкой ручного запуска загрузки
- `POST /settings/apk-notif` — пользователь переключает свои уведомления об APK

---

## 5. Сводная таблица путей доставки APK

| Сценарий | Путь |
|---|---|
| GitHub Actions завершился | 1) `POST /webhook/apk-binary` — бинарь напрямую (100-200 КБ/с, Amvera LAN) |
| | 2) `POST /webhook/apk-release` — метаданные + фоновая загрузка из GitHub |
| Первый запуск сервера без кеша | `on_event("startup")` → GitHub Releases API → скачать |
| Пользователь нажал «Скачать» без кеша | `GET /download/android` → запустить фоновую загрузку + страница ожидания с авторефрешем каждые 6 с |
| Пользователь нажал «Скачать» с кешем | `GET /download/android` → `FileResponse` прямо с диска |

---

## 6. Критические gotchas

1. **Приватный репо — browser_download_url даёт 404 с токеном.** Решение: резолвить через GitHub API Tags endpoint → взять `asset["url"]` (API-URL) → качать с `Accept: application/octet-stream`.

2. **Amvera рестартует при деплое кода.** GitHub Actions шлёт бинарь в том же пуше. Решение: `--retry 12 --retry-delay 20` в curl — ждёт до 4 минут пока сервер поднимется.

3. **Secrets нельзя в `if:` условии шага GitHub Actions.** Использовать shell: `[ -n "$SECRET" ] || { echo "skip"; exit 0; }`.

4. **bubblewrap CLI не работает в CI.** Только прямой Gradle build из `android/`. Требует `android/gradle.properties` с `android.useAndroidX=true`.

5. **Атомарная запись файла.** Всегда: сначала пишем в `.tmp`, потом `rename` → файл никогда не отдаётся в промежуточном состоянии.

6. **Минимальный размер для валидации.** `_APK_MIN_SIZE = 200_000` (200 KB). Реальные TWA APK ~500–600 KB. Защищает от пустых ответов и html-страниц с ошибкой.

7. **`data/apk/` должен быть на persistent volume.** В `amvera.yml`: `persistenceMount: /app/data`. Без этого APK удаляется при каждом деплое.

8. **`FileResponse` читает файл в момент запроса.** Не держи путь в переменной, проверяй `exists()` + `stat().st_size` перед отдачей.

9. **Уведомления Telegram из webhook — через `asyncio.run_coroutine_threadsafe`.** Вебхук обрабатывается в веб-event-loop (uvicorn), бот работает в отдельном loop (`_main_loop`). Передать корутину нужно через `run_coroutine_threadsafe(coro, _main_loop)`.

10. **APK_WEBHOOK_SECRET — один токен, два места:** Replit Secrets (`APK_WEBHOOK_SECRET`) и GitHub Secrets в репозитории (тоже `APK_WEBHOOK_SECRET`). Значения должны совпадать.

---

## 7. Минимальный чеклист для аналогичной реализации

- [ ] `data/apk/` на persistent volume (`amvera.yml`)
- [ ] `APK_WEBHOOK_SECRET` в Replit Secrets + GitHub Secrets репозитория
- [ ] `GITHUB_TOKEN` в Replit Secrets (для startup-авторазагрузки с GitHub)
- [ ] `KEYSTORE_BASE64` + `KEYSTORE_PASSWORD` в GitHub Secrets
- [ ] Маршруты: `GET /download/android`, `POST /webhook/apk-binary`, `POST /webhook/apk-release`
- [ ] Таблицы БД: `payment_settings`, `apk_release_history`, `apk_notif_prefs`
- [ ] `on_event("startup")` — авторазагрузка если кеша нет
- [ ] GitHub Actions: Java 17 + android SDK + Gradle (НЕ bubblewrap) + curl retry + release
