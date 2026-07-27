#!/bin/bash
# Post-merge setup script — запускается автоматически после слияния задач агентов.
# Идемпотентен, неинтерактивен, set -e.
set -e

echo "=== Post-merge setup ==="

# Устанавливаем/обновляем Python-зависимости
echo "1. Установка зависимостей..."
pip install -q -r requirements.txt

# Создаём папку data/ если вдруг нет
mkdir -p data

echo "=== Готово ==="
