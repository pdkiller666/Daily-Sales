---
name: Chat swipe hint pill dismissal
description: Why the chat group↔DM swipe hint pill kept lingering and how to make any in-place (non-navigating) swipe hint reliably dismiss
---

# Chat swipe hint pill dismissal

The chat group↔DM swipe shows a hint pill (`💬 Личные сообщения` / `💬 Групповой чат`) while dragging. It kept staying visible after the swipe completed.

**Two root causes, fixed together:**

1. **opacity transition is unreliable when there is no page navigation.** The navbar hint (base.html) hides via `opacity:0` and only *appears* to work because a navbar swipe triggers a full page navigation that destroys the DOM — the fade never has to complete. The chat swipe does NOT navigate (just an Alpine `dmMode` toggle), and the synchronous Alpine re-render + forced reflow (`_nudgeDmPanel` `void el.offsetHeight`) can interrupt the just-started opacity transition, leaving the pill stuck. Use `display:none` for in-place hides, not an opacity transition.

2. **Tie the hide to the completing ACTION, not just the touch event.** The navbar hides its hint on the action that completes the gesture (navigation). The chat's equivalent completing action is `switchToDm()` / `switchToGroup()`. Calling the hide there guarantees the pill disappears after every successful swipe even if `touchend`/`touchcancel` never fire (browser intercepting the gesture as edge-swipe-back / horizontal scroll). Touch-event hides alone are not enough.

**Also:** give the pill a stable element id (`ds-chat-swipe-hint`) and hide by `getElementById`, so any code path (closure or not) hits the same element and re-initialized components don't leave orphan pills in `<body>`.

**Why:** spent 3 deploy cycles on touch-event-only fixes that all failed; the durable rule is *in-place swipe hints must hide on the state-change, with display:none, via a stable id* — not via opacity on touchend.

**How to apply:** any future in-place (non-navigating) swipe/drag affordance — hide on the state mutation that the gesture triggers, use display toggling, and target a stable id.

## Overlapping pills (DM→group): suppress the navbar swipe inside the chat

Symptom: during a chat swipe, TWO pills stack at `left:16px` — the chat's own (`💬 Групповой чат ←`) and the navbar's (`Отчёты ←`).

Cause: the navbar swipe handler (base.html, on `document`) and the chat swipe handler (on `#chat-wrapper`) both run on the same `touchmove`. The chat handler only called `stopImmediatePropagation()` on `touchend`, never on touchstart/touchmove, so the navbar handler also drew its own pill during the drag.

Fix: the chat wrapper handlers `e.stopPropagation()` on **touchstart AND touchmove** so the gesture never bubbles to the navbar handler on `document`. touchstart-stop keeps the navbar from setting its `_ts`; touchmove-stop keeps the navbar from drawing its pill on a stale `_ts`. The chat fully owns horizontal swipes inside `#chat-wrapper` (group→DM, DM→group, group→back), so the navbar swipe is correctly inert there. The bottom-nav swipe-up (open More) is unaffected — its target is the nav bar, outside the wrapper.

**Rule:** when two swipe handlers live on nested elements (child + document), the child must stopPropagation on the WHOLE gesture (touchstart+touchmove+touchend), not just touchend — otherwise the ancestor's touchmove still fires and renders competing UI mid-drag.

Note: SW (`web/static/sw.js`) is network-first for navigations and never caches page HTML, so chat inline-JS changes are NOT subject to SW cache — deploy lag on Amvera is the only staleness source.
