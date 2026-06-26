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

Note: SW (`web/static/sw.js`) is network-first for navigations and never caches page HTML, so chat inline-JS changes are NOT subject to SW cache — deploy lag on Amvera is the only staleness source.
