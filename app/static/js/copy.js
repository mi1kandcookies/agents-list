/* copy.js - copy-to-clipboard buttons.
 *
 * Markup: <button type="button" data-copy="text to copy">Copy</button>
 * The button's text briefly changes to "Copied" (or "Press Ctrl+C" when the
 * clipboard API is unavailable, with the value selected in a prompt).
 */
(function () {
  'use strict';
  function flash(btn, text) {
    var label = btn.querySelector('[data-copy-label]') || btn;
    var before = label.textContent;
    label.textContent = text;
    btn.classList.add('is-copied');
    setTimeout(function () { label.textContent = before; btn.classList.remove('is-copied'); }, 1400);
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-copy]');
    if (!btn) return;
    e.preventDefault();
    var value = btn.getAttribute('data-copy');
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(value).then(function () { flash(btn, 'Copied'); },
        function () { window.prompt('Copy this value', value); });
    } else {
      window.prompt('Copy this value', value);
    }
  });
})();
