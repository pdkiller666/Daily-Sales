---
name: TWA APK build (GitHub Actions)
description: Как правильно собирать Android TWA APK в GitHub Actions — bubblewrap не работает, используем прямой Gradle build.
---

## Правило

**НЕ использовать bubblewrap в GitHub Actions.** Bubblewrap CLI (`@bubblewrap/cli`) стабильно падает с ошибкой `cli ERROR The provided androidSdk isn't correct` на любой конфигурации runner (25+ попыток), включая `android-actions/setup-android@v3`. Причина не воспроизводится и не чинится.

**Использовать прямой Gradle build** с Android-проектом в `android/`.

**Why:** bubblewrap имеет жёсткую валидацию Android SDK path (`cmdline-tools/latest/bin/sdkmanager`), которая не проходит ни с pre-installed SDK, ни с android-actions/setup-android@v3. Прямой Gradle build не зависит от bubblewrap CLI и стабильно работает.

## How to apply

**Структура проекта:**
```
android/
  settings.gradle
  build.gradle
  gradle.properties          ← android.useAndroidX=true ОБЯЗАТЕЛЬНО
  app/
    build.gradle             ← AGP 8.2.2, namespace, signingConfigs via env vars
    src/main/
      AndroidManifest.xml    ← TWA LauncherActivity + DelegationService
      res/values/strings.xml
      res/values/colors.xml
```

**Workflow ключевые шаги:**
```yaml
- uses: android-actions/setup-android@v3
- run: sdkmanager "platforms;android-34" "build-tools;34.0.0" && yes | sdkmanager --licenses
- uses: gradle/actions/setup-gradle@v3   ← НЕ gradle/setup-gradle (не существует)
  with:
    gradle-version: '8.4'
- run: |
    echo "${{ secrets.KEYSTORE_BASE64 }}" | base64 -d > dailysales-release.p12
- run: |
    gradle :app:assembleRelease --project-dir android --no-daemon
  env:
    KEYSTORE_PASSWORD: ${{ secrets.KEYSTORE_PASSWORD }}
    KEYSTORE_PATH: ${{ github.workspace }}/dailysales-release.p12
```

**gradle.properties обязателен:**
```
android.useAndroidX=true
android.enableJetifier=true
```
Без него — `checkReleaseAarMetadata FAILED` (AndroidX dependency но useAndroidX не включён).

**app/build.gradle — signing через env:**
```gradle
signingConfigs {
    release {
        storeFile new File(System.getenv('KEYSTORE_PATH') ?: '...')
        storePassword System.getenv('KEYSTORE_PASSWORD') ?: ''
        keyAlias 'dailysales'
        keyPassword System.getenv('KEYSTORE_PASSWORD') ?: ''
    }
}
```

**Логирование:** `secrets` нельзя в `if:` условии шага → проверять через env var в shell-скрипте. Для диагностики использовать `set +e; gradle ... > build.log 2>&1; BUILD_EXIT=$?; set -e; tail -120 build.log`.

**APK path после сборки:** `android/app/build/outputs/apk/release/app-release.apk`

## Webhook URL — критичный gotcha

Webhook в `build-twa.yml` должен указывать на **реальный Amvera-домен**:
```
https://dailysalesdeploy-pdkiller666.amvera.io/webhook/apk-release
```
НЕ на `dailysales.app` (домен не существует). Ошибка привела к тому, что все сборки отправляли webhook в никуда, APK никогда не скачивался на persistent volume Amvera, и `/download/android` всегда делал redirect на GitHub вместо прямой отдачи файла.

**Why:** Если webhook не доходит до сервера → `data/apk/DailySales-latest.apk` не создаётся → FileResponse недоступен → fallback на GitHub CDN (работает, но медленнее и требует Github аккаунт у пользователя для скачивания).

**How to apply:** При любом изменении webhook URL в workflow — сразу проверять curl вручную: `curl -I https://<domain>/webhook/apk-release`

## Android app icon

Иконки создаются из `web/static/icon-512.png` (512x512 RGBA) в mipmap-{mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/:
- `ic_launcher.png` — стандартная квадратная
- `ic_launcher_round.png` — круглая (через pixel-level mask)
- `ic_launcher_fg.png` — foreground для adaptive icon
- `mipmap-anydpi-v26/ic_launcher.xml` — adaptive icon XML (background=#2563EB + foreground)
AndroidManifest.xml: `android:icon="@mipmap/ic_launcher"` + `android:roundIcon="@mipmap/ic_launcher_round"`
