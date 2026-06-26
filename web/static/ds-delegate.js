/* ══════════════════════════════════════════════════════════════════
   ds-delegate.js — CSP-safe event delegation.

   Under the strict nonce-based CSP (script-src 'nonce-…' WITHOUT
   'unsafe-inline') the browser blocks ALL inline event-handler
   attributes (onclick/onchange/oninput/onsubmit) — there is no way to
   put a nonce on an attribute handler. This module replaces them with
   document-level delegated listeners driven by data-* attributes, so
   the interface keeps working while injected <script>/handlers stay
   blocked.

   Loaded as an external file from 'self' (allowed under nonce CSP).
   Works on dynamically inserted DOM too (delegation on `document`).

   Supported attributes
   ────────────────────
   click:
     data-href="URL"               navigate (skips clicks on inner controls)
     data-action="fn"              call window.fn(arg?, arg2?, el, event)
       data-arg / data-arg2          extra args (numeric strings → Number)
     data-modal-open="id"          #id.classList.remove('hidden')
     data-modal-close="id"         #id.classList.add('hidden')
     data-backdrop-close[="id"]    hide id (or self) only if click hit backdrop
     data-backdrop-action="fn"     call window.fn only if click hit backdrop
     data-toggle-dark              window.dsToggleDark()
     data-print                    window.print()
     data-reload                   location.reload()
     data-confirm="msg"            confirm(); cancel → preventDefault
     data-once="label"             dsOnce(el, label) (disable submit btn)
     data-stop                     marks an inner control so data-href skips it
   change:
     data-autosubmit               el.form.submit()
     data-change="fn"              call window.fn(arg?, arg2?, el, event)
     data-set-value="targetId"     #targetId.value = this.value
   input:
     data-input-transform="…"      upper | upper-code | int-min0 | autoheight
   submit:
     data-confirm="msg"            on a <form>: confirm(); cancel → preventDefault
   ══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* dsOnce — safe disable of a submit button: validate first, then block.
     setTimeout(0) defers the disable until AFTER the browser dispatches
     submit, otherwise mobile Chrome cancels the submission. Defined here
     (guarded) so standalone pages without base.html still get it. */
  if (typeof window.dsOnce !== 'function') {
    window.dsOnce = function (btn, label) {
      var f = btn ? btn.closest('form') : null;
      if (f && typeof f.checkValidity === 'function' && !f.checkValidity()) {
        f.reportValidity();
        return false;
      }
      if (btn) {
        var b = btn, l = label;
        setTimeout(function () { b.disabled = true; b.innerHTML = l || '⏳ Сохранение…'; }, 0);
      }
    };
  }

  if (typeof window.dsSetLs !== 'function') {
    window.dsSetLs = function (key, val) {
      try { localStorage.setItem(key, val == null ? '' : String(val)); } catch (e) {}
    };
  }
  if (typeof window.dsRmLs !== 'function') {
    window.dsRmLs = function (key) {
      try { localStorage.removeItem(key); } catch (e) {}
    };
  }

  function coerce(v) {
    if (v === undefined || v === null) return v;
    return /^-?\d+$/.test(v) ? Number(v) : v;
  }

  function dispatch(name, el, event) {
    var fn = window[name];
    if (typeof fn !== 'function') return undefined;
    var args = [];
    if (el.dataset.arg !== undefined) args.push(coerce(el.dataset.arg));
    if (el.dataset.arg2 !== undefined) args.push(coerce(el.dataset.arg2));
    args.push(el, event);
    try { return fn.apply(el, args); }
    catch (err) { try { console.error('ds-action ' + name, err); } catch (_) {} }
  }

  /* ───────── click ───────── */
  document.addEventListener('click', function (e) {
    var t = e.target;

    var open = t.closest('[data-modal-open]');
    if (open) {
      var oe = document.getElementById(open.getAttribute('data-modal-open'));
      if (oe) oe.classList.remove('hidden');
    }

    var close = t.closest('[data-modal-close]');
    if (close) {
      var ce = document.getElementById(close.getAttribute('data-modal-close'));
      if (ce) ce.classList.add('hidden');
    }

    var bd = t.closest('[data-backdrop-close]');
    if (bd && e.target === bd) {
      var bid = bd.getAttribute('data-backdrop-close');
      var be = bid ? document.getElementById(bid) : bd;
      if (be) be.classList.add('hidden');
    }

    var bda = t.closest('[data-backdrop-action]');
    if (bda && e.target === bda) {
      dispatch(bda.getAttribute('data-backdrop-action'), bda, e);
    }

    if (t.closest('[data-toggle-dark]') && typeof window.dsToggleDark === 'function') {
      window.dsToggleDark();
    }
    if (t.closest('[data-print]')) { window.print(); }
    if (t.closest('[data-reload]')) { window.location.reload(); }

    var cf = t.closest('[data-confirm]');
    if (cf && cf.tagName !== 'FORM') {
      if (!window.confirm(cf.getAttribute('data-confirm'))) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
    }

    var cp = t.closest('[data-copy]');
    if (cp && navigator.clipboard) {
      navigator.clipboard.writeText(cp.getAttribute('data-copy'));
      if (cp.hasAttribute('data-copy-feedback')) {
        var old = cp.textContent;
        cp.textContent = '✅';
        setTimeout(function () { cp.textContent = old; }, 1200);
      }
    }

    var sf = t.closest('[data-submit-form]');
    if (sf) {
      var fm = document.getElementById(sf.getAttribute('data-submit-form'));
      if (fm) { fm.submit(); }
    }

    var once = t.closest('[data-once]');
    if (once && typeof window.dsOnce === 'function') {
      if (window.dsOnce(once, once.getAttribute('data-once')) === false) {
        e.preventDefault();
        return;
      }
    }

    var act = t.closest('[data-action]');
    if (act) { dispatch(act.getAttribute('data-action'), act, e); }

    var nav = t.closest('[data-href]');
    if (nav) {
      var inter = t.closest(
        'a,button,input,select,textarea,label,' +
        '[data-stop],[data-noclk],[data-action],[data-modal-open],' +
        '[data-modal-close],[data-confirm],[data-once]'
      );
      if (!inter || inter === nav || !nav.contains(inter)) {
        window.location.href = nav.getAttribute('data-href');
      }
    }
  });

  /* ───────── change ───────── */
  document.addEventListener('change', function (e) {
    var as = e.target.closest('[data-autosubmit]');
    if (as && as.form) { as.form.submit(); return; }

    var sv = e.target.closest('[data-set-value]');
    if (sv) {
      var tgt = document.getElementById(sv.getAttribute('data-set-value'));
      if (tgt) tgt.value = sv.value;
    }

    var ch = e.target.closest('[data-change]');
    if (ch) { dispatch(ch.getAttribute('data-change'), ch, e); }
  });

  /* ───────── input ───────── */
  document.addEventListener('input', function (e) {
    var el = e.target.closest('[data-input-transform]');
    if (!el) return;
    var m = el.getAttribute('data-input-transform');
    if (m === 'upper') {
      el.value = el.value.toUpperCase();
    } else if (m === 'upper-code') {
      el.value = el.value.toUpperCase().replace(/[^A-Z0-9А-ЯЁ\-_]/g, '');
    } else if (m === 'int-min0') {
      el.value = Math.max(0, parseInt(el.value || 0, 10) || 0);
    } else if (m === 'autoheight') {
      el.style.height = 'auto';
      el.style.height = el.scrollHeight + 'px';
    }
  });

  /* ───────── submit ───────── */
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f || !f.hasAttribute) return;
    if (f.hasAttribute('data-confirm')) {
      if (!window.confirm(f.getAttribute('data-confirm'))) { e.preventDefault(); return; }
    }
    if (f.hasAttribute('data-submit')) {
      var fn = window[f.getAttribute('data-submit')];
      if (typeof fn === 'function') {
        var a = [e];
        if (f.dataset.arg !== undefined) a.push(coerce(f.dataset.arg));
        a.push(f);
        try { fn.apply(f, a); }
        catch (err) { try { console.error('ds-submit', err); } catch (_) {} }
      }
    }
  });
})();
