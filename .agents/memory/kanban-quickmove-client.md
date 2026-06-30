---
name: Kanban quick-move client-side sync
description: How kanban.html relocateCard/updateColumnCount sync counts/WIP after a move, and the testing trap it creates
---

# Kanban quick-move client-side sync (kanban.html)

After a quick move (group_by=none), the board updates **client-side only** via
`relocateCard()` + `updateColumnCount()` — no reload. Grouped/swimlane view still
does a full `window.location.reload()`.

## Testing trap: hidden current-column button renders FIRST
Each card renders a quick-move button for **every** column, hiding the current
column's button with inline `style="display:none"` (so a move-back option exists
after a client move). Because the column order starts at `new`, a `new`-status
card's FIRST `button[data-action="moveCard"]` is the hidden `new` button.

**How to apply:** any Playwright/Selenium selector that clicks "a quick-move
button" must use `:visible` (e.g. `button[data-action='moveCard']:visible`), not
`.first` — `.first` grabs the hidden current-column button and the click fails
with "Element is not visible". This bit `check_kanban_quick_move` in
`test_web_smoke.py` after the client-side refactor.

## What updateColumnCount does and does NOT sync
On WIP change it only toggles the **badge** red classes (`bg-red-100`,
`dark:bg-red-900`, `text-red-700`, `dark:text-red-300`). It does NOT update the
column header ring (`ring-2 ring-red-400`) nor the "⚠️ Превышен WIP-лимит"
banner — those stay stale until a reload. Also a column rendered over-WIP (badge
has only red classes, no base color) loses all color when it drops back under
limit, since the base color class was never present to restore.
