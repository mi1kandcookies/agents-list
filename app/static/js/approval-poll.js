/* approval-poll.js - live state for /approvals/<id>.
 *
 * Polls GET /api/approvals/<id> (which also advances device flows) until the
 * approval reaches a terminal state, then reloads so the server renders the
 * final screen. Also drives the expiry countdown and the Cancel button.
 * Markup contract: #approval[data-api][data-cancel][data-state][data-terminal]
 * [data-interval][data-expires-at], [data-countdown], [data-cancel-button].
 */
(function () {
  'use strict';
  var root = document.getElementById('approval');
  if (!root) return;

  var state = root.dataset.state;
  var terminal = root.dataset.terminal === 'true';
  var paymentPending = root.dataset.paymentPending === 'true';
  var expiresAt = parseInt(root.dataset.expiresAt, 10) || 0;
  var intervalMs = Math.max(2, parseInt(root.dataset.interval, 10) || 3) * 1000;
  var TERMINAL = ['consumed', 'denied', 'expired', 'cancelled', 'rejected', 'blocked', 'failed'];

  function tick() {
    var left = Math.max(0, expiresAt - Math.floor(Date.now() / 1000));
    var text = Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0');
    document.querySelectorAll('[data-countdown]').forEach(function (el) { el.textContent = text; });
  }

  function poll() {
    fetch(root.dataset.api, { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.state !== state) { window.location.reload(); return; }
        if (data && data.poll_interval) intervalMs = Math.max(2, data.poll_interval) * 1000;
        paymentPending = !!(data && data.payment_pending);
        if (!data || TERMINAL.indexOf(data.state) === -1 || paymentPending) setTimeout(poll, intervalMs);
      })
      .catch(function () { setTimeout(poll, intervalMs * 2); });
  }

  document.querySelectorAll('[data-cancel-button]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      btn.disabled = true;
      fetch(root.dataset.cancel, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ reason: 'cancelled on the approval page' })
      }).then(function () { window.location.reload(); });
    });
  });

  // Start the identity handoff in a deliberate browser popup. A human click
  // is required, so popup blockers do not turn an agent proposal into an
  // authorization. The main approval page keeps polling independently and
  // renders the final on-chain receipt after the server consumes the approval.
  document.querySelectorAll('[data-approval-popup]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = btn.dataset.approvalPopup;
      if (!target) return;
      var popup = window.open(target, 'agents-list-approval',
        'popup=yes,width=480,height=760,resizable=yes,scrollbars=yes');
      var note = document.querySelector('[data-popup-note]');
      var fallback = document.querySelector('[data-popup-fallback]');
      if (popup) {
        try { popup.focus(); } catch (_) {}
        if (note) { note.hidden = false; note.textContent = 'Approval window opened. Keep this page open while the provider checks your approval.'; }
      } else {
        if (fallback) fallback.hidden = false;
        if (note) { note.hidden = false; note.textContent = 'Your browser blocked the approval window. Use “Open in a new tab” or allow popups for this site.'; }
      }
    });
  });

  if (!terminal) {
    tick();
    setInterval(tick, 1000);
    // Approved-but-not-executed also polls, so the page updates once it runs.
    setTimeout(poll, state === 'pending' || state === 'approved' ? intervalMs : 1000);
  }
})();
