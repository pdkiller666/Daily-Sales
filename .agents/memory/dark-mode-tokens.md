---
name: Dark mode tokens & !important removal
description: Why base.html dark overrides keep !important on element/input/table rules but not class-utility rules
---

Тёмная тема в `web/templates/base.html` живёт в inline `<style>` как глобальные
оверрайды Tailwind-утилит под `html.dark`. Палитра вынесена в CSS-токены
`html.dark{--ds-900..--ds-200, --ds-accent, ...}`.

**Правило про `!important`:** снимать `!important` можно ТОЛЬКО с
class-utility-оверрайдов вида `html.dark .bg-white` / `.text-slate-*` /
`.border-*` — их специфичность (0,2,1) и так перебивает Tailwind `dark:`-утилиту
(`.dark .dark\:bg-x` = (0,2,0)), поэтому рендер не меняется.

**Оставлять `!important`** на element-/input-/table-правилах:
`html.dark input/select/textarea`, `thead th`, `tbody tr/td`, `label`,
`footer`, `aside.fixed`, `header.sticky`, `nav.fixed`. У них специфичность
element-уровня (напр. `html.dark input` = (0,1,2)) НИЖЕ, чем у `dark:`-утилиты
класса (0,2,0) → без `!important` `dark:bg-...` на инпуте выиграет и сломает
тему.

**Why:** roadmap 4.3 просил «убрать лишние !important + токены, не трогая 69
шаблонов». Просто массовое удаление `!important` дало бы регрессию на формах и
таблицах, где элементы несут `dark:`-классы.

**How to apply:** при добавлении нового dark-оверрайда — если селектор
`html.dark .<один-класс>`, `!important` не нужен; если селектор цепляется к тегу
(input/td/th/label/...), `!important` обязателен.
