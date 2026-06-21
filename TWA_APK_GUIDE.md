# Android TWA APK — сборка через GitHub Actions

> Инструкция написана на основе реального production-опыта проекта DailySales (25+ итераций).
> Содержит критически важные «грабли», на которые уже наступили — чтобы ты не наступал заново.

---

## Что такое TWA и зачем

**Trusted Web Activity (TWA)** — нативная Android-обёртка для PWA-сайта. Пользователь получает APK-файл, устанавливает его как обычное приложение, получает иконку на рабочем столе, работу без адресной строки — а под капотом открывается твой сайт в Chrome.

**Условие работы TWA:** сайт обязан отдавать файл `/.well-known/assetlinks.json`, подписанный тем же SHA-256 отпечатком, что и APK. Без этого Chrome покажет адресную строку — TWA не активируется.

**Итоговый результат:**
- GitHub Actions собирает APK при каждом push
- APK публикуется в GitHub Releases автоматически
- Опционально: сервер получает APK по webhook и раздаёт его через `/download/android`

---

## ⚠️ ГЛАВНАЯ ЛОВУШКА (прочитай до начала)

**`bubblewrap` CLI НЕ РАБОТАЕТ в GitHub Actions.**

Симптом: постоянная ошибка `androidSdk isn't correct` независимо от версии JDK, SDK, способа установки.

**Решение:** прямая сборка через Gradle из заранее подготовленного `android/` проекта + `gradle/actions/setup-gradle@v3`. Это единственный надёжный путь.

---

## Шаг 1. Создание подписного ключа (keystore)

Делается один раз локально (или в терминале). Ключ нужно сохранить навсегда — им подписываются все будущие версии APK.

```bash
keytool -genkey -v \
  -keystore myapp-release.p12 \
  -storetype PKCS12 \
  -alias myapp \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000 \
  -storepass YOUR_KEYSTORE_PASSWORD \
  -keypass YOUR_KEYSTORE_PASSWORD \
  -dname "CN=MyApp, OU=Dev, O=MyCompany, L=Moscow, ST=Moscow, C=RU"
```

После генерации — получить SHA-256 отпечаток (нужен для assetlinks.json):

```bash
keytool -list -v \
  -keystore myapp-release.p12 \
  -storetype PKCS12 \
  -storepass YOUR_KEYSTORE_PASSWORD \
  | grep "SHA256:"
```

Сохрани отпечаток в формате `XX:XX:XX:...:XX` (32 пары hex, разделённые двоеточиями).

Закодируй keystore в base64 для GitHub Secret:

```bash
base64 myapp-release.p12 | tr -d '\n'
# или через Python:
python3 -c "import base64; print(base64.b64encode(open('myapp-release.p12','rb').read()).decode())"
```

**Важно:** файл `.p12` никогда не коммить в репозиторий (добавь в `.gitignore`).

---

## Шаг 2. Структура Android-проекта

Создай папку `android/` в корне репозитория со следующей структурой:

```
android/
├── app/
│   ├── build.gradle
│   └── src/main/
│       ├── AndroidManifest.xml
│       └── res/
│           ├── mipmap-anydpi-v26/
│           │   ├── ic_launcher.xml
│           │   └── ic_launcher_round.xml
│           ├── mipmap-hdpi/
│           │   ├── ic_launcher.png
│           │   ├── ic_launcher_round.png
│           │   └── ic_launcher_fg.png
│           ├── mipmap-mdpi/   (те же файлы)
│           ├── mipmap-xhdpi/  (те же файлы)
│           ├── mipmap-xxhdpi/ (те же файлы)
│           ├── mipmap-xxxhdpi/(те же файлы)
│           └── values/
│               ├── strings.xml
│               └── colors.xml
├── build.gradle
├── settings.gradle
└── gradle.properties
```

### `android/settings.gradle`

```groovy
rootProject.name = 'myapp'
include ':app'
```

### `android/gradle.properties`

```properties
android.useAndroidX=true
android.enableJetifier=true
org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
```

> **Критично:** `android.useAndroidX=true` обязателен. Без него сборка падает с ошибкой совместимости.

### `android/build.gradle`

```groovy
buildscript {
    repositories {
        google()
        mavenCentral()
    }
    dependencies {
        classpath 'com.android.tools.build:gradle:8.1.4'
    }
}

allprojects {
    repositories {
        google()
        mavenCentral()
    }
}
```

### `android/app/build.gradle`

```groovy
plugins {
    id 'com.android.application'
}

android {
    namespace 'com.example.myapp'        // ← твой package ID
    compileSdk 34

    defaultConfig {
        applicationId 'com.example.myapp'   // ← твой package ID
        minSdk 19
        targetSdk 34
        versionCode 1
        versionName '1.0'
    }

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_11
        targetCompatibility JavaVersion.VERSION_11
    }

    signingConfigs {
        release {
            storeFile new File(System.getenv('KEYSTORE_PATH') ?: "${rootDir}/../../myapp-release.p12")
            storePassword System.getenv('KEYSTORE_PASSWORD') ?: ''
            keyAlias 'myapp'             // ← alias из keytool -alias
            keyPassword System.getenv('KEYSTORE_PASSWORD') ?: ''
        }
    }

    buildTypes {
        release {
            minifyEnabled false
            signingConfig signingConfigs.release
        }
    }
}

dependencies {
    implementation 'com.google.androidbrowserhelper:androidbrowserhelper:2.5.0'
}
```

---

## Шаг 3. AndroidManifest.xml

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <!-- Добавляй только нужные разрешения -->
    <!-- Если сайт использует камеру (сканер штрихкодов, QR и т.п.): -->
    <uses-permission android:name="android.permission.CAMERA" />
    <uses-feature android:name="android.hardware.camera" android:required="false" />
    <uses-feature android:name="android.hardware.camera.autofocus" android:required="false" />

    <application
        android:label="@string/app_name"
        android:icon="@mipmap/ic_launcher"
        android:roundIcon="@mipmap/ic_launcher_round"
        android:allowBackup="true"
        android:supportsRtl="true">

        <activity
            android:name="com.google.androidbrowserhelper.trusted.LauncherActivity"
            android:exported="true">

            <!-- Стартовая страница после запуска -->
            <meta-data
                android:name="android.support.customtabs.trusted.DEFAULT_URL"
                android:value="https://ТВОЙ_ДОМЕН/dashboard" />

            <!-- Цвет строки состояния -->
            <meta-data
                android:name="android.support.customtabs.trusted.STATUS_BAR_COLOR"
                android:resource="@color/colorPrimary" />

            <!-- Длительность fadeout сплэш-экрана (мс) -->
            <meta-data
                android:name="android.support.customtabs.trusted.SPLASH_SCREEN_FADE_OUT_DURATION"
                android:value="300" />

            <!-- Запуск из лаунчера -->
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>

            <!-- Digital Asset Links verification — критично для TWA -->
            <intent-filter android:autoVerify="true">
                <action android:name="android.intent.action.VIEW" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data
                    android:scheme="https"
                    android:host="ТВОЙ_ДОМЕН" />
            </intent-filter>
        </activity>

        <service
            android:name="com.google.androidbrowserhelper.trusted.DelegationService"
            android:exported="true">
            <intent-filter>
                <action android:name="android.support.customtabs.trusted.TRUSTED_WEB_ACTIVITY_SERVICE" />
                <category android:name="android.intent.category.DEFAULT" />
            </intent-filter>
        </service>

    </application>
</manifest>
```

### `res/values/strings.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">MyApp</string>
</resources>
```

### `res/values/colors.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="colorPrimary">#2563eb</color>
    <color name="colorBackground">#f8fafc</color>
    <color name="ic_launcher_background">#2563EB</color>
</resources>
```

### Adaptive icon (`mipmap-anydpi-v26/ic_launcher.xml`)

```xml
<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background"/>
    <foreground android:mipmap="ic_launcher_fg"/>
</adaptive-icon>
```

### Иконки — требуемые размеры

| Папка | `ic_launcher.png` |
|---|---|
| `mipmap-mdpi` | 48×48 px |
| `mipmap-hdpi` | 72×72 px |
| `mipmap-xhdpi` | 96×96 px |
| `mipmap-xxhdpi` | 144×144 px |
| `mipmap-xxxhdpi` | 192×192 px |

`ic_launcher_round.png` — те же размеры, круглая версия.
`ic_launcher_fg.png` — foreground для adaptive icon (512×512, прозрачный фон, безопасная зона 66%).

---

## Шаг 4. assetlinks.json на сервере

Это связывающий файл — без него TWA не работает (показывается адресная строка Chrome).

Сервер **обязан** отдавать по пути `/.well-known/assetlinks.json` ответ со SHA-256 отпечатком из keystore.

### FastAPI (Python)

```python
@app.get("/.well-known/assetlinks.json", include_in_schema=False)
async def assetlinks():
    from fastapi.responses import JSONResponse
    data = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.example.myapp",    # ← твой package ID
            "sha256_cert_fingerprints": [
                "XX:XX:XX:XX:..."    # ← SHA-256 из keytool (шаг 1)
            ]
        }
    }]
    return JSONResponse(data, headers={"Cache-Control": "no-cache"})
```

### Flask (Python)

```python
@app.route("/.well-known/assetlinks.json")
def assetlinks():
    import json
    data = [{"relation": ["delegate_permission/common.handle_all_urls"],
             "target": {"namespace": "android_app",
                        "package_name": "com.example.myapp",
                        "sha256_cert_fingerprints": ["XX:XX:XX:..."]}}]
    return app.response_class(json.dumps(data), mimetype="application/json",
                              headers={"Cache-Control": "no-cache"})
```

### Express.js (Node)

```js
app.get('/.well-known/assetlinks.json', (req, res) => {
    res.setHeader('Cache-Control', 'no-cache');
    res.json([{
        relation: ['delegate_permission/common.handle_all_urls'],
        target: {
            namespace: 'android_app',
            package_name: 'com.example.myapp',
            sha256_cert_fingerprints: ['XX:XX:XX:...']
        }
    }]);
});
```

> **Проверка:** после деплоя открой `https://ТВОЙ_ДОМЕН/.well-known/assetlinks.json` в браузере — должен отдаваться JSON.
> Дополнительная проверка через [Digital Asset Links API](https://developers.google.com/digital-asset-links/tools/generator).

---

## Шаг 5. GitHub Actions workflow

Создай файл `.github/workflows/build-twa.yml`:

```yaml
name: Build TWA APK

on:
  push:
    branches: [main, master]
    paths:
      - 'android/**'
      - 'twa-manifest.json'
      - '.github/workflows/build-twa.yml'
  workflow_dispatch:   # ручной запуск из интерфейса GitHub

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: write    # нужно для создания Release

    steps:
      - uses: actions/checkout@v4

      - name: Setup Java 17
        uses: actions/setup-java@v4
        with:
          java-version: '17'
          distribution: 'temurin'

      - name: Setup Android SDK
        uses: android-actions/setup-android@v3

      - name: Install SDK components & accept licenses
        run: |
          sdkmanager "platforms;android-34" "build-tools;34.0.0" 2>/dev/null || true
          yes | sdkmanager --licenses 2>/dev/null || true

      - name: Setup Gradle
        uses: gradle/actions/setup-gradle@v3
        with:
          gradle-version: '8.4'

      - name: Decode keystore
        run: |
          echo "${{ secrets.KEYSTORE_BASE64 }}" | base64 -d > "${{ github.workspace }}/myapp-release.p12"
          SIZE=$(wc -c < "${{ github.workspace }}/myapp-release.p12")
          echo "Keystore size: ${SIZE} bytes"
          [ "$SIZE" -gt 100 ] || { echo "ERROR: KEYSTORE_BASE64 invalid"; exit 1; }

      - name: Build APK
        run: |
          gradle :app:assembleRelease \
            --project-dir android \
            --no-daemon \
            --info > build.log 2>&1
          tail -80 build.log
        env:
          KEYSTORE_PASSWORD: ${{ secrets.KEYSTORE_PASSWORD }}
          KEYSTORE_PATH: ${{ github.workspace }}/myapp-release.p12

      - name: Find built APK
        id: find_apk
        run: |
          APK=$(find android -name "*.apk" | grep release | grep -v unsigned | head -1)
          if [ -z "$APK" ]; then APK=$(find android -name "*.apk" | head -1); fi
          echo "Found: $APK"
          [ -n "$APK" ] || { echo "No APK produced"; exit 1; }
          echo "apk_path=$APK" >> $GITHUB_OUTPUT

      - name: Rename APK
        run: |
          FINAL="MyApp-v1.0.${{ github.run_number }}.apk"
          cp "${{ steps.find_apk.outputs.apk_path }}" "$FINAL"
          echo "APK_FILE=$FINAL" >> $GITHUB_ENV

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: MyApp-APK-${{ github.run_number }}
          path: ${{ env.APK_FILE }}
          retention-days: 30

      - name: Create GitHub Release
        id: create_release
        uses: softprops/action-gh-release@v2
        with:
          tag_name: apk-${{ github.run_number }}
          name: "MyApp APK — сборка #${{ github.run_number }}"
          body: |
            ## Автоматическая TWA-сборка

            **Коммит:** `${{ github.sha }}`
            **Ветка:** `${{ github.ref_name }}`

            ### Установка на Android (разовая настройка)
            1. Скачайте APK-файл ниже
            2. Откройте на телефоне → разрешите «Установку из неизвестных источников»
            3. Установите — иконка появится на рабочем столе

            ### Обновление
            Просто установите новый APK поверх старого — данные сохранятся.
          files: ${{ env.APK_FILE }}
          draft: false
          prerelease: false
```

---

## Шаг 6. GitHub Secrets

В репозитории → **Settings → Secrets and variables → Actions → New repository secret**:

| Имя секрета | Значение |
|---|---|
| `KEYSTORE_BASE64` | Вывод команды `base64 myapp-release.p12` |
| `KEYSTORE_PASSWORD` | Пароль, указанный при `keytool -storepass` |

**Опционально** (для webhook-уведомления сервера):

| `APK_WEBHOOK_SECRET` | Случайная строка — тот же токен, что прописан на сервере |

---

## Шаг 7. Исключение `.github/` из деплоя на сервер

Папку `.github/` нужно пушить на GitHub (там живёт workflow), но **не нужно** деплоить на продакшн-сервер (Amvera, Railway и т.п.).

В скрипте деплоя добавь исключение при синхронизации файлов. Пример для rsync:

```bash
rsync -av --exclude='.github/' ./  user@server:/app/
```

Или при ручном копировании — просто не включай `.github/` в архив/синхронизацию для сервера.

---

## Шаг 8 (опциональный). Серверная раздача APK

Вместо того чтобы пользователь шёл на GitHub Releases, сервер может кешировать APK и раздавать по удобному URL (`/download/android`).

### Схема

```
GitHub Actions → POST /webhook/apk-binary (двоичный APK) → сохраняется на диск
Пользователь  → GET  /download/android   → сервер отдаёт файл с диска или 302 на GitHub
```

### Приём APK по webhook (FastAPI)

```python
import os
from pathlib import Path
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse

_APK_LOCAL = Path("data/apk/myapp-latest.apk")
_APK_MIN_SIZE = 200_000  # 200 KB минимум для валидного APK


@app.post("/webhook/apk-binary", include_in_schema=False)
async def webhook_apk_binary(request: Request):
    secret = os.environ.get("APK_WEBHOOK_SECRET", "")
    auth = request.headers.get("Authorization", "")
    if not secret or auth != f"Bearer {secret}":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.body()
    if len(body) < _APK_MIN_SIZE:
        return JSONResponse({"error": f"Too small: {len(body)} bytes"}, status_code=400)
    _APK_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    _APK_LOCAL.write_bytes(body)
    return {"ok": True, "size": len(body)}


@app.get("/download/android", include_in_schema=False)
async def download_android():
    if _APK_LOCAL.exists() and _APK_LOCAL.stat().st_size >= _APK_MIN_SIZE:
        return FileResponse(
            str(_APK_LOCAL),
            media_type="application/vnd.android.package-archive",
            filename="MyApp.apk",
        )
    # Fallback на GitHub Releases
    return RedirectResponse("https://github.com/USER/REPO/releases/latest", status_code=302)
```

### Отправка APK в workflow (добавить шаг после Create Release)

```yaml
      - name: Upload APK binary to server
        env:
          APK_WEBHOOK_SECRET: ${{ secrets.APK_WEBHOOK_SECRET }}
        run: |
          [ -n "$APK_WEBHOOK_SECRET" ] || { echo "Secret not set, skipping"; exit 0; }
          STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            --retry 12 --retry-delay 20 --retry-max-time 300 \
            --retry-all-errors --retry-connrefused \
            -X POST "https://ТВОЙ_ДОМЕН/webhook/apk-binary" \
            -H "Authorization: Bearer ${APK_WEBHOOK_SECRET}" \
            -H "Content-Type: application/octet-stream" \
            --data-binary "@${{ env.APK_FILE }}" --max-time 120 || echo "000")
          echo "HTTP status: ${STATUS}"
          [ "${STATUS}" = "200" ] || { echo "Upload failed (HTTP ${STATUS})"; exit 1; }
```

> **Важно:** `--retry-connrefused` нужен, если сервер одновременно перезапускается при деплое кода (race condition с webhook из того же push).

---

## Шаг 9. twa-manifest.json (справочный файл)

Файл `twa-manifest.json` в корне — документация параметров проекта. В GitHub Actions сборке **не участвует** (используется только при запуске bubblewrap вручную). Полезен как справочник и для истории.

```json
{
  "packageId": "com.example.myapp",
  "host": "ТВОЙ_ДОМЕН",
  "name": "MyApp Full Name",
  "launcherName": "MyApp",
  "display": "standalone",
  "themeColor": "#2563eb",
  "backgroundColor": "#f8fafc",
  "startUrl": "/dashboard",
  "iconUrl": "https://ТВОЙ_ДОМЕН/static/icon-512.png",
  "minSdkVersion": 19,
  "sdkVersion": 33,
  "targetSdkVersion": 34,
  "appVersionName": "1.0.0",
  "fingerprints": [
    {
      "name": "release",
      "value": "XX:XX:XX:...:XX"
    }
  ]
}
```

---

## Проверочный чеклист перед первой сборкой

- [ ] `android/gradle.properties` содержит `android.useAndroidX=true`
- [ ] `android/app/build.gradle`: `namespace` и `applicationId` совпадают с `package_name` в assetlinks
- [ ] `AndroidManifest.xml`: `android:host` совпадает с реальным доменом сайта
- [ ] Keystore сгенерирован, SHA-256 отпечаток получен через `keytool -list -v`
- [ ] `/.well-known/assetlinks.json` отдаётся сервером — проверено в браузере
- [ ] SHA-256 в `assetlinks.json` === SHA-256 из `keytool`
- [ ] GitHub Secrets: `KEYSTORE_BASE64` и `KEYSTORE_PASSWORD` добавлены
- [ ] Иконки в 5 размерах присутствуют в `mipmap-*/`
- [ ] `.github/` исключено из деплоя на продакшн-сервер
- [ ] Workflow запущен вручную (`workflow_dispatch`) для первой проверки

---

## Полный список файлов

```
android/
├── app/
│   ├── build.gradle                          ← зависимость androidbrowserhelper
│   └── src/main/
│       ├── AndroidManifest.xml               ← LauncherActivity + intent-filter
│       └── res/
│           ├── mipmap-anydpi-v26/
│           │   ├── ic_launcher.xml           ← adaptive icon descriptor
│           │   └── ic_launcher_round.xml
│           ├── mipmap-{hdpi,mdpi,xhdpi,xxhdpi,xxxhdpi}/
│           │   ├── ic_launcher.png
│           │   ├── ic_launcher_round.png
│           │   └── ic_launcher_fg.png
│           └── values/
│               ├── strings.xml               ← app_name
│               └── colors.xml                ← colorPrimary
├── build.gradle                              ← Android Gradle plugin
├── settings.gradle                           ← include ':app'
└── gradle.properties                         ← useAndroidX=true (КРИТИЧНО)

.github/workflows/
└── build-twa.yml                             ← CI: build + release + webhook

twa-manifest.json                             ← документация параметров (опционально)
```

---

## Частые ошибки и решения

| Ошибка | Причина | Решение |
|---|---|---|
| `androidSdk isn't correct` | bubblewrap CLI | Не использовать bubblewrap — только прямой Gradle |
| Адресная строка в TWA | assetlinks.json не работает | Проверить URL, SHA-256, Cache-Control |
| SHA256 mismatch | Пересоздан keystore | Нельзя пересоздавать ключ — SHA изменится, обновить assetlinks |
| APK слишком маленький | Неверный keystore или пустой файл | Проверить KEYSTORE_BASE64, размер после decode |
| `secrets` нельзя в `if:` шага | Ограничение GitHub Actions | Проверять через env-переменную в shell (`[ -n "$SECRET" ]`) |
| `KEYSTORE_PATH` не найден | env var не передан в Gradle | Убедиться что блок `env:` в шаге Build APK содержит `KEYSTORE_PATH` |
| Сервер не получает webhook | Race condition с деплоем | Использовать `--retry 12 --retry-delay 20 --retry-connrefused` |
| Приватный репо: APK 404 | `browser_download_url` требует токен | Качать через GitHub API с `Accept: application/octet-stream` + токен |

---

## Ключевые версии (проверено в production)

| Компонент | Версия |
|---|---|
| Java | 17 (temurin) |
| Android SDK | compileSdk 34, minSdk 19, targetSdk 34 |
| Gradle | 8.4 |
| Android Gradle Plugin | 8.1.4 |
| `androidbrowserhelper` | 2.5.0 |
| `actions/checkout` | v4 |
| `actions/setup-java` | v4 |
| `android-actions/setup-android` | v3 |
| `gradle/actions/setup-gradle` | v3 |
| `actions/upload-artifact` | v4 |
| `softprops/action-gh-release` | v2 |
