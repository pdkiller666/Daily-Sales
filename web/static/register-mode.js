/* register-mode.js — CSP-safe toggle of registration mode sections on /register.
   Wired via data-change="dsSetRegMode" on the mode radios (ds-delegate.js). */
(function () {
  function apply(mode) {
    document.querySelectorAll('[data-mode-section]').forEach(function (el) {
      var modes = el.getAttribute('data-mode-section').split(',');
      var show = modes.indexOf(mode) !== -1;
      el.classList.toggle('hidden', !show);
      el.querySelectorAll('[data-mode-required]').forEach(function (input) {
        if (show) {
          input.setAttribute('required', 'required');
        } else {
          input.removeAttribute('required');
        }
      });
    });
    document.querySelectorAll('[data-mode-label]').forEach(function (el) {
      var modes = el.getAttribute('data-mode-label').split(',');
      el.classList.toggle('ds-mode-active', modes.indexOf(mode) !== -1);
    });
  }

  window.dsSetRegMode = function (el) {
    apply(el && el.value);
  };

  document.addEventListener('DOMContentLoaded', function () {
    var checked = document.querySelector('input[name="usage_mode"]:checked');
    apply(checked ? checked.value : 'join');
  });
})();
