---
    name: Bare csrf_token template variable requires explicit route context
    description: Explains why {{ csrf_token }} silently renders empty and breaks POST forms if a route forgets to pass it, and the two conventions in use.
    ---

    Two CSRF-token conventions coexist in this codebase's Jinja templates:
    1. Bare `{{ csrf_token }}` — a plain context variable, NOT a Jinja global. Used by the majority of templates. Every route that renders such a template MUST explicitly put `"csrf_token": get_csrf_token(request)` (from web/auth.py) in its TemplateResponse context dict, or Jinja's default (non-strict) undefined renders it as an empty string with no error.
    2. `{{ csrf_token_for(request) }}` — a registered Jinja global (`templates.env.globals['csrf_token_for']`), used by a smaller set of templates (e.g. products/*). Works without any route-side wiring.

    **Why:** A silent empty token is not a crash — the form still renders and submits normally. The failure only appears server-side as `verify_csrf_token()` rejecting the request and redirecting to `...?error=csrf`, which the UI shows as a generic "Произошла ошибка" banner. This looks identical to an unrelated bug and is easy to misdiagnose as a permissions or JS issue. Root-caused twice already: once in the packages module (packages_list/packages_sold GET routes never added "csrf_token" to context, despite their templates using the bare variable).

    **How to apply:** When adding a new page/module with forms, or when debugging a mysterious "ошибка" / "?error=csrf" redirect on form submit:
    - `grep -n "csrf_token" web/templates/<module>/*.html` to see which convention the templates use.
    - If bare `{{ csrf_token }}`: `grep -n "csrf_token" web/routes/<module>.py` and confirm every GET route rendering that template includes `"csrf_token": get_csrf_token(request)` in its context dict (import `from web.auth import get_csrf_token`).
    - Routes that build context via a shared helper (e.g. appointments.py's `_get_ctx()`) are safe by construction — only ad-hoc per-route context dicts are at risk.
    