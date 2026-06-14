"""
Startup wrapper for DailySales bot.
Checks for required environment variables before launching.
"""
import os
import sys
import socket
import signal
import subprocess
from dotenv import load_dotenv

load_dotenv('data/.env', override=False)

def _free_port(port: int) -> None:
    """Kill any process occupying the given port before binding."""
    try:
        result = subprocess.run(
            ["fuser", f"{port}/tcp"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        if pids:
            import time; time.sleep(0.8)
    except FileNotFoundError:
        pass  # fuser not available

_free_port(5000)

BOT_TOKEN = os.getenv('BOT_TOKEN')

if not BOT_TOKEN:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse

    app = FastAPI()

    @app.get("/{path:path}")
    async def setup_page(request: Request, path: str = ""):
        html = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DailySales — Настройка</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
  .card { background: #1e293b; border-radius: 16px; padding: 40px; max-width: 560px;
          width: 100%; box-shadow: 0 25px 50px rgba(0,0,0,0.5); }
  h1 { color: #38bdf8; margin: 0 0 8px; font-size: 1.8rem; }
  .subtitle { color: #94a3b8; margin: 0 0 32px; }
  .step { background: #0f172a; border-radius: 10px; padding: 20px; margin: 12px 0; }
  .step-num { background: #38bdf8; color: #0f172a; font-weight: 700; width: 28px; height: 28px;
              border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
              font-size: 0.85rem; margin-bottom: 10px; }
  .step h3 { margin: 0 0 6px; color: #f1f5f9; font-size: 1rem; }
  .step p { margin: 0; color: #94a3b8; font-size: 0.9rem; line-height: 1.5; }
  code { background: #334155; color: #7dd3fc; padding: 2px 7px; border-radius: 5px; font-size: 0.85em; }
  .warn { background: #451a03; border: 1px solid #92400e; border-radius: 10px;
          padding: 16px; margin-top: 24px; color: #fcd34d; font-size: 0.9rem; }
</style>
</head>
<body>
<div class="card">
  <h1>🤖 DailySales</h1>
  <p class="subtitle">Требуется начальная настройка</p>

  <div class="step">
    <div class="step-num">1</div>
    <h3>Создайте Telegram-бота</h3>
    <p>Напишите <code>@BotFather</code> в Telegram, выполните <code>/newbot</code> и скопируйте токен.</p>
  </div>

  <div class="step">
    <div class="step-num">2</div>
    <h3>Добавьте секреты в Replit</h3>
    <p>Откройте вкладку <strong>Secrets</strong> (🔒) в Replit и добавьте:<br>
    <code>BOT_TOKEN</code> — токен от BotFather<br>
    <code>ADMIN_CHAT_ID</code> — ваш Telegram ID (узнайте у <code>@userinfobot</code>)</p>
  </div>

  <div class="step">
    <div class="step-num">3</div>
    <h3>Перезапустите приложение</h3>
    <p>После добавления секретов нажмите <strong>Stop</strong> и <strong>Run</strong> в Replit.</p>
  </div>

  <div class="warn">
    ⚠️ <strong>BOT_TOKEN не найден.</strong> Приложение не может запуститься без токена Telegram-бота.
  </div>
</div>
</body>
</html>
"""
        return HTMLResponse(content=html)

    print("⚠️  BOT_TOKEN не задан — запускаем страницу настройки на порту 5000")
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="warning")
else:
    import asyncio
    import main as _main
    asyncio.run(_main.main())
