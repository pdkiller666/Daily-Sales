---
name: Chat date separators / grouping / unread divider
description: Frontend-only Telegram-style visuals in web chat — how separators, grouping and the unread anchor are derived
---

# Date separators, message grouping, unread divider (web chat)

All three live ONLY in `web/templates/chat/index.html` (Alpine), backend untouched.

## Time field
`created_at` arrives as `"DD.MM.YYYY HH:MM"` for BOTH group (`_fmt_msg`) and DM (`_fmt_ts`). No separate date field — derive everything from this string: `sameDay` compares `created_at.slice(0,10)`. Times are formatted server-side from UTC (no TZ conversion in chat), so the «Сегодня/Вчера» comparison against the browser's local date can drift near midnight — this matches the existing per-message timestamp behavior and is accepted.

## Grouping helpers
`_sameSender(a,b)` works for both lists: uses `user_id` when present (group), falls back to `is_mine` (DM). `firstInGroup` → show name (group only). `lastInGroup` → show avatar; non-last grouped rows render an empty spacer (`w-8` group / `width:28px` DM) to keep bubble alignment. Intra-group spacing is `mb-0.5`, group-end is `mb-3` (group) / `mb-2` (DM).

**Why a wrapper `<div>` around each x-for item:** Alpine `x-for` needs a single root; separators must be siblings of the message row, so each iteration is wrapped. Because of that, container-level uniform spacing was REMOVED (`space-y-3` on `#chat-scroll`, `gap-2` on `#dm-conv-scroll`) and ALL spacing moved to per-row `mb-*`. If you re-add `space-y`/`gap` to those containers it double-spaces and kills grouping tightness.

## Unread divider anchor
`groupUnreadAnchorId` / `dmUnreadAnchorId` = id of first-unread message, computed as `list[len - unreadCount]` (clamped to 0). Source of `unreadCount` is the EXISTING unread badge captured **before** it is reset to 0.
- **How to apply:** set the anchor in EVERY open path. There are THREE for groups: `init()` (landing topic), `switchTopic()`, and any future topic-open. DM: `openDmConversation()` (after `_dmFetchMessages`). Forgetting `init()` was the original miss — divider then never shows on the topic you land on directly.
- Approximation: if `unread > loaded window`, divider pins to the top of the loaded batch (not exact first-unread). Accepted; no "earlier unread" label was built (out of top-3 scope).
- Anchor is by message **id**, so DM prepend-pagination (`dmLoadMore`) doesn't shift it.

Live-append (poll/WS, incl. the manual DM WS bubble builder) needs no changes: separators/grouping recompute from the array as long as appended objects carry `created_at` + sender field (they do).
