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
        if (!data || TERMINAL.indexOf(data.state) === -1) setTimeout(poll, intervalMs);
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

  if (!terminal) {
    tick();
    setInterval(tick, 1000);
    // Approved-but-not-executed also polls, so the page updates once it runs.
    setTimeout(poll, state === 'pending' || state === 'approved' ? intervalMs : 1000);
  }
})();
