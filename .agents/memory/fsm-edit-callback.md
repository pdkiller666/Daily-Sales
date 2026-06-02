---
name: fsm_edit only for message handlers
description: fsm_edit() signature accepts message not callback; wrong usage in callback handlers causes errors
---

## Правило

`fsm_edit(message_or_callback, text, markup)` — функция принимает aiogram `Message` объект (или объект с аналогичным `.edit_text()` методом), но **не** `CallbackQuery`.

Попытка передать `callback` вместо `message` вызывает ошибку, потому что у `CallbackQuery` нет прямого `.edit_text()`.

```python
# НЕПРАВИЛЬНО — в callback-handler (вызовет AttributeError/TypeError):
@router.callback_query(F.data == 'abs_my')
async def abs_my(callback: CallbackQuery, state: FSMContext):
    await fsm_edit(callback, text, markup)  # ОШИБКА — callback ≠ message

# ПРАВИЛЬНО — в callback-handler:
@router.callback_query(F.data == 'abs_my')
async def abs_my(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

# ПРАВИЛЬНО — использование fsm_edit в message-handler:
@router.message(SomeState.waiting)
async def handler(message: Message, state: FSMContext):
    await fsm_edit(message, text, markup)  # OK — передаётся message
```

**Why:** В `absence_handlers.py` (сессия 345) три хендлера (`abs_my`, `abs_hist`, `abs_new`) неправильно вызывали `fsm_edit(callback, ...)`. Это не только вызывало ошибку, но и не вызывало `callback.answer()`, что приводило к «зависанию» кнопки в Telegram.

**How to apply:** В любом `@router.callback_query(...)` хендлере НЕ использовать `fsm_edit`. Всегда:
1. `await callback.answer()` (или `await callback.answer("текст", show_alert=True)`)
2. `await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")`
