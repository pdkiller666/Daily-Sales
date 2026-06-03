---
name: Alpine x-cloak pattern
description: Which x-show elements need x-cloak to prevent flash-of-visible-content before Alpine initializes
---

## Правило

`x-cloak` нужен только тогда, когда выполняются **все три условия**:
1. Элемент по умолчанию видим (нет `display:none` в CSS/HTML)
2. Alpine при инициализации **скрывает** его (`x-show` → false на начальном состоянии)
3. Элемент **заметно крупный** или критически важный (полноширинная кнопка, шторка, модал)

**Why:** Alpine инициализируется через ~10–30ms после загрузки страницы. За это время элемент виден при opacity ~3–10% (т.к. `main.ds-main` начинается с opacity:0). Мелкие инлайн-элементы (loading spinners, кнопки внутри форм) — вспышка практически незаметна.

**Канонический пример — POS-корзина:**  
```html
<!-- БЕЗ x-cloak: кнопка мигала при каждом переходе -->
<button x-show="cart.length > 0" ...>

<!-- С x-cloak: кнопка скрыта до Alpine, не мигает -->
<button x-show="cart.length > 0" x-cloak ...>
```

**`[x-cloak]{display:none!important}`** — определено в `base.html` line 28. Подключается глобально.

**How to apply:**
- Обязательно: модалы (`x-show="showModal"`), шторки, POS-корзина, hint-дивы (`x-show="showHint"`)
- Необязательно: элементы внутри модалов с `x-cloak` на родителе (родитель уже скрыт), мелкие loading-спиннеры, `x-show="!loading"` (начальный loading=false → элемент сразу виден = нет вспышки)
- Не нужен, если начальное состояние **true** (`x-show="!editing"` при `editing=false` → элемент показан сразу, нет рассинхрона)
