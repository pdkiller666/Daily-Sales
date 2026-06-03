---
name: CSS transform breaks position:fixed children
description: Applying transform to an ancestor of a position:fixed element creates a new containing block, breaking fixed positioning. Critical for ds-main animation.
---

## Правило

**Никогда не применять `transform` к предку `position:fixed` элемента.**

CSS-свойство `transform` (и `filter`, `will-change: transform`, `perspective`) создаёт новый containing block. Это ломает `position:fixed` у любых вложенных элементов — они позиционируются относительно трансформируемого предка, а не вьюпорта.

**Why:** В `main.ds-main` была анимация `@keyframes ds-fade-in` с `transform:translateY(-8px) → translateY(0)`. POS-корзина (`position:fixed; bottom:80px`) позиционировалась относительно `main`, а не экрана — «прыгала» при навигации.

**Fix applied:** `ds-fade-in` изменён на **opacity-only** анимацию (без transform). `ds-card-anim` (dashboard-карточки) использует transform — это безопасно, потому что у карточек нет fixed-потомков.

**How to apply:**
- `main.ds-main` — только `opacity: 0 → 1`, НИКАКОГО transform
- Если нужен slide-effect для другого контейнера — убедиться, что внутри нет `position:fixed` элементов
- POS, модалы, bottom sheet — всегда `position:fixed` относительно вьюпорта; их предки не должны иметь transform
