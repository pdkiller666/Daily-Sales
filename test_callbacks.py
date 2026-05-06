"""
Статический анализ callback-хендлеров.

Проверяет два класса ошибок:

  1. ДВОЙНОЙ ОТВЕТ — в теле функции есть безусловный callback.answer() В НАЧАЛЕ
     (top-level, вне if/try/for) И ещё один где-нибудь ниже.
     Взаимоисключающие ветки (if X: answer(); return ... answer()) — НЕ баг.

  2. ПРОПУЩЕННЫЙ ОТВЕТ — у функции ЕСТЬ декоратор .callback_query(...) и НЕТ
     ни одного callback.answer() нигде в теле. Хелперы без декоратора пропускаются.

Запуск: python test_callbacks.py
"""
import ast
import sys
import os
import glob


HANDLER_FILES = sorted(glob.glob('*_handlers.py') + ['handlers.py', 'subscription_router.py'])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_cb_answer(node) -> bool:
    """True если узел — `await callback.answer(...)`"""
    if not isinstance(node, ast.Await):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != 'answer':
        return False
    obj = func.value
    if isinstance(obj, ast.Name) and obj.id == 'callback':
        return True
    if isinstance(obj, ast.Attribute) and obj.attr == 'callback':
        return True
    return False


def _stmt_contains_cb_answer(stmt) -> bool:
    """Рекурсивно проверяет, есть ли в stmt callback.answer()."""
    for node in ast.walk(stmt):
        if isinstance(node, ast.Await) and _is_cb_answer(node):
            return True
    return False


def _is_top_level_cb_answer(stmt) -> bool:
    """True если stmt сам по себе `await callback.answer(...)`."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
        return _is_cb_answer(stmt.value)
    return False


def has_callback_query_decorator(func_node) -> bool:
    for deco in func_node.decorator_list:
        txt = ast.unparse(deco) if hasattr(ast, 'unparse') else repr(deco)
        if 'callback_query' in txt:
            return True
    return False


def count_all_cb_answers(func_node) -> int:
    count = 0
    for node in ast.walk(func_node):
        if isinstance(node, ast.Await) and _is_cb_answer(node):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Bug detection
# ---------------------------------------------------------------------------

def check_double_answer(func_node) -> bool:
    """
    Настоящий двойной ответ: в списке top-level statements функции есть
    безусловный `await callback.answer(...)` — И после него ещё один
    callback.answer() (внутри if-ветки или прямо в теле).

    Взаимоисключающая схема (if cond: answer(); return) НЕ считается багом,
    если все ответы либо внутри if-блоков с return/raise, либо в конце после
    всех проверок.
    """
    body = func_node.body

    # Ищем позицию первого top-level безусловного ответа
    first_unconditional_idx = None
    for i, stmt in enumerate(body):
        if _is_top_level_cb_answer(stmt):
            first_unconditional_idx = i
            break

    if first_unconditional_idx is None:
        return False  # нет безусловного top-level ответа

    # Проверяем: есть ли ещё callback.answer() ПОСЛЕ этого индекса?
    for stmt in body[first_unconditional_idx + 1:]:
        if _stmt_contains_cb_answer(stmt):
            return True

    return False


def _is_simple_await_stmt(stmt) -> bool:
    """True если stmt — `await some_func(...)` (не callback.answer)."""
    if not isinstance(stmt, ast.Expr):
        return False
    if not isinstance(stmt.value, ast.Await):
        return False
    call = stmt.value.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == 'answer':
        return False
    return True


def is_delegate_function(func_node) -> bool:
    """
    True если функция — заглушка-делегат: её тело состоит только из await-вызовов
    (без callback.answer()), причём последний вызов — передача (callback, ...) другому
    хендлеру. Паттерны совместимости и cleanup+delegate считаются делегатами.
    Примеры:
      • await other_handler(callback, state)
      • await clear_state_keep_org(state); await other_handler(callback, state)
    """
    body = func_node.body
    # Отбрасываем leading docstring
    stmts = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not stmts:
        return False
    # Все инструкции должны быть await-вызовами (не callback.answer)
    if not all(_is_simple_await_stmt(s) for s in stmts):
        return False
    # Последний await должен принимать callback как аргумент
    last_call = stmts[-1].value.value
    for arg in last_call.args:
        if isinstance(arg, ast.Name) and arg.id == 'callback':
            return True
    return False


def check_missing_answer(func_node) -> bool:
    """
    Пропущенный ответ: у функции есть декоратор callback_query,
    но нет ни одного callback.answer() в теле, и это не делегат.
    """
    if not has_callback_query_decorator(func_node):
        return False
    if is_delegate_function(func_node):
        return False
    return count_all_cb_answers(func_node) == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

results_double = []
results_missing = []
file_errors = []

print("=" * 65)
print("  Статический анализ callback-хендлеров")
print("=" * 65)

for filepath in HANDLER_FILES:
    if not os.path.exists(filepath):
        continue
    try:
        with open(filepath, encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except SyntaxError as e:
        file_errors.append((filepath, str(e)))
        continue

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue

        if check_double_answer(node):
            results_double.append((filepath, node.name, node.lineno))

        if check_missing_answer(node):
            results_missing.append((filepath, node.name, node.lineno))

total_issues = 0

print()
if results_double:
    print(f"  ⚠️  ДВОЙНОЙ ОТВЕТ ({len(results_double)} случаев):")
    for filepath, name, lineno in results_double:
        print(f"       {filepath}:{lineno}  {name}()")
    total_issues += len(results_double)
else:
    print("  ✅  Двойных ответов не найдено")

print()
if results_missing:
    print(f"  ⚠️  ПРОПУЩЕННЫЙ ОТВЕТ ({len(results_missing)} случаев):")
    for filepath, name, lineno in results_missing:
        print(f"       {filepath}:{lineno}  {name}()")
    total_issues += len(results_missing)
else:
    print("  ✅  Пропущенных ответов не найдено")

if file_errors:
    print()
    print(f"  ❌  Синтаксические ошибки ({len(file_errors)} файлов):")
    for filepath, err in file_errors:
        print(f"       {filepath}: {err}")
    total_issues += len(file_errors)

print()
print("=" * 65)
print(f"  Итог: {total_issues} проблем обнаружено")
print("=" * 65)

sys.exit(1 if total_issues or file_errors else 0)
