/* ── AgentHire - Main JS ────────────────────────────────────────────────────── */

// ── Scroll-reveal: fades in .anim-on-scroll elements as they enter viewport ──
(function initScrollReveal() {
  function activate(el) { el.classList.add('visible'); }

  if (!('IntersectionObserver' in window)) {
    document.querySelectorAll('.anim-on-scroll').forEach(activate);
    return;
  }

  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) { activate(e.target); io.unobserve(e.target); } });
  }, { threshold: 0.05, rootMargin: '0px 0px -20px 0px' });

  function observe() {
    document.querySelectorAll('.anim-on-scroll').forEach(el => {
      // If already in viewport (e.g. top of page), activate immediately
      const rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight && rect.bottom > 0) { activate(el); }
      else { io.observe(el); }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', observe);
  } else {
    observe();
  }
})();

// ── AgentHireAPI - shared fetch utility ──────────────────────────────────────
// Usage:
//   AgentHireAPI.get('/api/agents')           → Promise<Object>
//   AgentHireAPI.post('/api/x402/pay', body)  → Promise<Object>
// Errors are thrown with a readable message so callers can showToast on catch.
const AgentHireAPI = (() => {
  async function request(method, url, body) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data;
    try { data = await res.json(); } catch (_) { data = {}; }
    if (!res.ok) {
      const msg = data.error || data.message || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }
  return {
    get:  (url)        => request('GET',  url),
    post: (url, body)  => request('POST', url, body),
    put:  (url, body)  => request('PUT',  url, body),
    del:  (url)        => request('DELETE', url),
  };
})();
// Expose globally so inline <script> blocks can use it.
window.AgentHireAPI = AgentHireAPI;

// ── Toasts ───────────────────────────────────────────────────────────────────
function showToast(message, type = 'info', duration = 3500) {
  const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span class="toast-icon">${icons[type]}</span><span>${message}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('toast-out');
    setTimeout(() => toast.remove(), 200);
  }, duration);
}

// ── Modals ─────────────────────────────────────────────────────────────────────
function openModal(id) {
  const overlay = document.getElementById(id);
  if (overlay) { overlay.classList.add('open'); document.body.style.overflow = 'hidden'; }
}
function closeModal(id) {
  const overlay = document.getElementById(id);
  if (overlay) { overlay.classList.remove('open'); document.body.style.overflow = ''; }
}

// Defensive: if no modal is currently open on page load, make sure body scroll
// is not stuck in the hidden state from a stale session/back-button restore.
document.addEventListener('DOMContentLoaded', () => {
  if (!document.querySelector('.modal-overlay.open')) {
    document.body.style.overflow = '';
  }
});

document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    closeModal(e.target.id);
  }
  if (e.target.classList.contains('modal-close') || e.target.closest('.modal-close')) {
    const overlay = e.target.closest('.modal-overlay');
    if (overlay) closeModal(overlay.id);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    // entry-gate uses .entry-gate-overlay (not .modal-overlay) so it is never closed here
    document.querySelectorAll('.modal-overlay.open').forEach(m => closeModal(m.id));
  }
});

// ── Tabs ──────────────────────────────────────────────────────────────────────
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  const target  = btn.dataset.tab;
  const tabsWrap = btn.closest('.tabs-wrapper');
  if (!tabsWrap) return;
  tabsWrap.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  tabsWrap.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  const content = tabsWrap.querySelector(`[data-tab-content="${target}"]`);
  if (content) content.classList.add('active');
});

// ── Role Switcher ─────────────────────────────────────────────────────────────
const rolePaths = {
  buyer:  '/marketplace',
  seller: '/seller/earnings',
  agent:  '/sim',
  admin:  '/admin/dashboard',
};
document.querySelectorAll('.role-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.role-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const role = btn.dataset.role;
    if (rolePaths[role]) window.location.href = rolePaths[role];
  });
});

// ── Wallet Connect (delegates to web3.js AgentHire object) ───────────────────
// web3.js already handles the wallet-btn click via its own DOMContentLoaded
// listener. This block syncs the nav button state when the wallet connects
// through OTHER means (e.g. checkout page, auto-reconnect).
window.addEventListener('agenthire:connected', (e) => {
  const btn = document.getElementById('wallet-btn');
  if (btn && e.detail && e.detail.address) {
    const short = e.detail.address.slice(0, 6) + '\u2026' + e.detail.address.slice(-4);
    btn.innerHTML = `<span style="font-family:var(--font-mono)">${short}</span>`;
    btn.setAttribute('data-connected', 'true');
  }
});

// ── Live Price Ticker (agent detail page) ────────────────────────────────────
function startLivePriceTicker(agentId) {
  const priceEl = document.getElementById('live-price');
  if (!priceEl) return;

  async function tick() {
    try {
      const data = await AgentHireAPI.get(`/api/price/${agentId}`);
      const formatted = data.price < 1 ? data.price.toFixed(5) : data.price.toFixed(4);
      priceEl.textContent = `$${formatted}`;
      priceEl.style.color = 'var(--green)';
      setTimeout(() => { priceEl.style.color = ''; }, 600);
    } catch (_) {}
  }
  tick();
  setInterval(tick, 4000);
}

// ── Live Ticker Strip (base nav) ───────────────────────────────────────────
// Polls /api/price/<id> for each agent referenced by the ticker and fills in
// the price. IDs come from data-agent-id which is set in the Jinja template.
(function initTickerStripPolling() {
  const POLL_MS = 15000;

  function uniqueIds() {
    return [...new Set([...document.querySelectorAll('.ticker-item[data-agent-id]')]
      .map(el => el.dataset.agentId).filter(Boolean))];
  }

  async function pollTicker() {
    const ids = uniqueIds();
    if (!ids.length) return;
    for (const id of ids) {
      try {
        const data = await AgentHireAPI.get('/api/price/' + id);
        const display = '$' + (data.price < 1 ? data.price.toFixed(5) : data.price.toFixed(4));
        document.querySelectorAll('.ticker-item[data-agent-id="' + id + '"] .ticker-price')
          .forEach(el => { el.textContent = display; });
        const panelPrice = document.getElementById('panel-price-' + id);
        if (panelPrice) panelPrice.textContent = display;
      } catch (_) { /* ignore single-agent failures */ }
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!document.querySelector('.ticker-item[data-agent-id]')) return;
    pollTicker();
    setInterval(pollTicker, POLL_MS);
  });
})();

// ── Tier Selection ────────────────────────────────────────────────────────────
// Scope selection clearing to the card's own group (.form-step if present,
// otherwise the containing .grid-2) so clicking a billing card doesn't
// deselect the verification tier on multi-step forms like /seller/create.
document.querySelectorAll('.tier-card').forEach(card => {
  card.addEventListener('click', () => {
    // Scope selection to the closest grid so sibling groups (billing / verify /
    // stake) don't clear each other's selection inside the same form step.
    const group = card.closest('.grid-2, .grid-3, .grid-4') || card.closest('.form-step') || document;
    group.querySelectorAll('.tier-card').forEach(c => c.classList.remove('selected'));
    card.classList.add('selected');
    const tier = card.dataset.tier;
    // Only write to the hidden tier field for verification tiers, never billing.
    if (tier === 'basic' || tier === 'thorough') {
      const hidden = document.getElementById('selected-tier');
      if (hidden) hidden.value = tier;
      const priceEl = document.getElementById('verification-price');
      if (priceEl) priceEl.textContent = tier === 'thorough' ? '$50.00 USDC' : '$10.00 USDC';
    }
    // Stake tier selection - writes to #selected-stake for the create form.
    const stake = card.dataset.stake;
    if (stake) {
      const hidden = document.getElementById('selected-stake');
      if (hidden) hidden.value = stake;
    }
  });
});

// ── Star Rating ───────────────────────────────────────────────────────────────
function initStarRating(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const stars = container.querySelectorAll('.star-btn');
  stars.forEach((star, idx) => {
    star.addEventListener('mouseenter', () => {
      stars.forEach((s, i) => s.classList.toggle('active', i <= idx));
    });
    star.addEventListener('click', () => {
      stars.forEach((s, i) => {
        s.classList.toggle('active', i <= idx);
        s.dataset.selected = i <= idx ? 'true' : 'false';
      });
      const ratingInput = document.getElementById('rating-value');
      if (ratingInput) ratingInput.value = idx + 1;
    });
  });
  container.addEventListener('mouseleave', () => {
    const selected = [...stars].findIndex(s => s.dataset.selected === 'true' && [...stars].slice(stars.length - 1)[0] === s);
    const activeStars = [...stars].filter(s => s.dataset.selected === 'true');
    if (activeStars.length === 0) stars.forEach(s => s.classList.remove('active'));
  });
}

// ── Multi-step Form ───────────────────────────────────────────────────────────
let currentStep = 1;
const totalSteps = 4;

function goToStep(step) {
  if (step < 1 || step > totalSteps) return;
  document.querySelectorAll('.form-step').forEach((el, i) => {
    el.style.display = i + 1 === step ? 'block' : 'none';
  });
  document.querySelectorAll('.step-item').forEach((el, i) => {
    el.classList.remove('active', 'done');
    if (i + 1 < step) el.classList.add('done');
    if (i + 1 === step) el.classList.add('active');
  });
  currentStep = step;
  const prevBtn = document.getElementById('step-prev');
  const nextBtn = document.getElementById('step-next');
  const submitBtn = document.getElementById('step-submit');
  if (prevBtn)   prevBtn.style.display  = step > 1 ? 'inline-flex' : 'none';
  if (nextBtn)   nextBtn.style.display  = step < totalSteps ? 'inline-flex' : 'none';
  if (submitBtn) submitBtn.style.display = step === totalSteps ? 'inline-flex' : 'none';
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

const nextBtn = document.getElementById('step-next');
const prevBtn = document.getElementById('step-prev');
if (nextBtn) nextBtn.addEventListener('click', () => {
  goToStep(currentStep + 1);
  showToast('Progress saved', 'success');
});
if (prevBtn) prevBtn.addEventListener('click', () => goToStep(currentStep - 1));

// Note: #confirm-pay click is handled by the real x402 handler in checkout.html.

// ── Admin action buttons ───────────────────────────────────────────────────
// Each button carries a data-* attribute with the entity id (data-vrf-id,
// data-payout-id, data-report-id). We POST to the matching Flask route and
// update the row in place on success.
async function _adminAction(btn, url, successMsg, errorMsg, onSuccess) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = 'Working...';
  try {
    const res = await AgentHireAPI.post(url, {});
    showToast(successMsg, 'success');
    if (typeof onSuccess === 'function') onSuccess(btn, res);
  } catch (err) {
    showToast(errorMsg + ': ' + err.message, 'error');
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

function _disableCard(btn) {
  const card = btn.closest('.card');
  if (card) {
    card.querySelectorAll('button').forEach(b => b.disabled = true);
    card.style.opacity = '0.6';
  }
}

document.addEventListener('click', (e) => {
  const approve     = e.target.closest('.approve-btn[data-vrf-id]');
  const reject      = e.target.closest('.reject-btn[data-vrf-id]');
  const startTest   = e.target.closest('.start-testing-btn[data-vrf-id]');
  const escalate    = e.target.closest('.escalate-btn[data-vrf-id]');
  const release     = e.target.closest('.release-btn[data-payout-id]');
  const hold        = e.target.closest('.hold-btn[data-payout-id]');
  const refund      = e.target.closest('.refund-btn[data-payout-id]');
  const releaseAll  = e.target.closest('#release-all-btn');
  const investigate = e.target.closest('.investigate-btn[data-report-id]');
  const suspend     = e.target.closest('.suspend-btn[data-report-id]');
  const resolve     = e.target.closest('.resolve-btn[data-report-id]');

  if (approve) {
    const id = approve.dataset.vrfId;
    _adminAction(approve, '/admin/verification-queue/' + id + '/approve',
      id + ' approved', 'Approve failed', _disableCard);
  } else if (reject) {
    const id = reject.dataset.vrfId;
    _adminAction(reject, '/admin/verification-queue/' + id + '/reject',
      id + ' rejected', 'Reject failed', _disableCard);
  } else if (startTest) {
    const id = startTest.dataset.vrfId;
    _adminAction(startTest, '/admin/verification-queue/' + id + '/test-start',
      'Testing started for ' + id, 'Could not start testing',
      () => setTimeout(() => window.location.reload(), 900));
  } else if (escalate) {
    const id = escalate.dataset.vrfId;
    _adminAction(escalate, '/admin/verification-queue/' + id + '/escalate',
      id + ' escalated to human review', 'Escalate failed',
      () => setTimeout(() => window.location.reload(), 900));
  } else if (release) {
    const id = release.dataset.payoutId;
    _adminAction(release, '/admin/payouts/' + id + '/release',
      'Payout ' + id + ' released', 'Release failed', (btn) => {
        const row = btn.closest('tr');
        const statusCell = row && row.querySelector('td:nth-child(6)');
        if (statusCell) statusCell.innerHTML = '<span class="badge badge-approved">Released</span>';
        const actionCell = btn.closest('td');
        if (actionCell) actionCell.innerHTML = '<span class="text-xs text-muted">Complete</span>';
      });
  } else if (hold) {
    const id = hold.dataset.payoutId;
    _adminAction(hold, '/admin/payouts/' + id + '/hold',
      'Payout ' + id + ' held', 'Hold failed', (btn) => {
        const row = btn.closest('tr');
        const statusCell = row && row.querySelector('td:nth-child(6)');
        if (statusCell) statusCell.innerHTML = '<span class="badge badge-rejected">On Hold</span>';
        const actionCell = btn.closest('td');
        if (actionCell) actionCell.innerHTML = '<span class="text-xs text-muted">Held</span>';
      });
  } else if (refund) {
    if (!confirm('Refund this payout to the buyer? This cannot be undone.')) return;
    const id = refund.dataset.payoutId;
    _adminAction(refund, '/admin/payouts/' + id + '/refund',
      'Refund issued for ' + id, 'Refund failed', (btn) => {
        const actionCell = btn.closest('td');
        if (actionCell) actionCell.innerHTML = '<span class="text-xs text-muted">Refunded</span>';
      });
  } else if (releaseAll) {
    if (!confirm('Release all pending payouts? This cannot be undone.')) return;
    _adminAction(releaseAll, '/admin/payouts/release-all',
      'Bulk release complete', 'Bulk release failed',
      () => setTimeout(() => window.location.reload(), 900));
  } else if (investigate) {
    const id = investigate.dataset.reportId;
    _adminAction(investigate, '/admin/moderation/' + id + '/investigate',
      id + ' now investigating', 'Could not update',
      () => setTimeout(() => window.location.reload(), 900));
  } else if (suspend) {
    if (!confirm('Suspend this agent pending review?')) return;
    const id = suspend.dataset.reportId;
    _adminAction(suspend, '/admin/moderation/' + id + '/suspend',
      'Agent suspended on ' + id, 'Suspend failed',
      () => setTimeout(() => window.location.reload(), 900));
  } else if (resolve) {
    const id = resolve.dataset.reportId;
    _adminAction(resolve, '/admin/moderation/' + id + '/resolve',
      id + ' resolved', 'Resolve failed',
      () => setTimeout(() => window.location.reload(), 900));
  }
});

// ── Search (client-side filter hint) ──────────────────────────────────────────
const searchInput = document.getElementById('marketplace-search');
if (searchInput) {
  let debounce;
  searchInput.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      const form = searchInput.closest('form');
      if (form) form.submit();
    }, 500);
  });
}

// ── Surge badge tooltip ────────────────────────────────────────────────────────
document.querySelectorAll('.badge-surge').forEach(badge => {
  badge.title = 'Price adjusted due to peak demand';
});

// ── Order completion + rating ──────────────────────────────────────────────────
// Real handlers live in templates/order.html (#mark-complete calls
// /api/orders/<id>/complete with a confirm dialog) and templates/base.html
// (#submit-rating posts to /api/agents/<id>/rate). We intentionally skip
// registering anything here to avoid duplicate toasts and premature modal opens.

// ── SubagentWorkflow - stage expand/collapse ──────────────────────────────────
document.addEventListener('click', (e) => {
  const trigger = e.target.closest('.stage-trigger');
  if (!trigger) return;
  const item = trigger.closest('.stage-item');
  if (!item) return;
  const isOpen = item.classList.contains('open');
  // close all in same list
  item.closest('.stage-list')?.querySelectorAll('.stage-item').forEach(i => i.classList.remove('open'));
  if (!isOpen) item.classList.add('open');
});

// ── ProcessingSummary toggle ───────────────────────────────────────────────────
document.addEventListener('click', (e) => {
  const hdr = e.target.closest('.processing-summary-header');
  if (!hdr) return;
  const body = hdr.nextElementSibling;
  if (!body) return;
  const isHidden = body.style.display === 'none' || body.style.display === '';
  body.style.display = isHidden ? 'block' : 'none';
  const chevron = hdr.querySelector('.summary-chevron');
  if (chevron) chevron.style.transform = isHidden ? 'rotate(90deg)' : '';
});

// ── SubagentEditor ─────────────────────────────────────────────────────────────
let stageCount = 0;

function initSubagentEditor() {
  const toggle    = document.getElementById('subagent-toggle');
  const panel     = document.getElementById('subagent-editor-panel');
  if (!toggle || !panel) return;

  toggle.addEventListener('click', () => {
    const sw = toggle.querySelector('.toggle-switch');
    const on = sw?.classList.toggle('on');
    panel.style.display = on ? 'block' : 'none';
    const label = toggle.querySelector('.toggle-label');
    if (label) label.textContent = on ? 'Multi-stage with sub-agents' : 'Standalone (no sub-agents)';
    updateEditorPreview();
  });
}

function addStage(name = '', purpose = '', type = 'internal') {
  stageCount++;
  const list = document.getElementById('stage-editor-list');
  if (!list) return;
  const id = 'stage-' + stageCount;
  const el = document.createElement('div');
  el.className = 'stage-editor-item';
  el.dataset.stageId = id;
  el.innerHTML = `
    <div class="stage-editor-header">
      <span class="stage-editor-handle">⠿</span>
      <span class="stage-editor-order">Stage ${list.children.length + 1}</span>
      <span class="stage-editor-name">${name || 'New Stage'}</span>
      <button class="btn btn-ghost btn-sm" style="padding:3px 8px; margin-left:auto;" onclick="removeStage(this)">✕</button>
    </div>
    <div class="stage-editor-body">
      <div class="form-group">
        <label class="form-label">Stage Name</label>
        <input type="text" class="form-input stage-name-input" value="${name}" placeholder="e.g. Input Parser"
               oninput="this.closest('.stage-editor-item').querySelector('.stage-editor-name').textContent = this.value || 'New Stage'; updateEditorPreview();">
      </div>
      <div class="form-group">
        <label class="form-label">Purpose</label>
        <input type="text" class="form-input stage-purpose-input" value="${purpose}" placeholder="What this stage does..." oninput="updateEditorPreview()">
      </div>
      <div class="form-group">
        <label class="form-label">Type</label>
        <div class="stage-type-toggle">
          <button class="stage-type-btn ${type === 'internal' ? 'active internal' : ''}" onclick="setStageType(this,'internal')">Internal</button>
          <button class="stage-type-btn ${type === 'subagent' ? 'active subagent' : ''}" onclick="setStageType(this,'subagent')">Sub-Agent</button>
        </div>
      </div>
      <label class="filter-option">
        <input type="checkbox" ${type === 'subagent' ? '' : 'disabled'} class="stage-conditional"> Conditional (only called when needed)
      </label>
    </div>
  `;
  list.appendChild(el);
  renumberStages();
  updateEditorPreview();
}

function removeStage(btn) {
  btn.closest('.stage-editor-item')?.remove();
  renumberStages();
  updateEditorPreview();
}

function setStageType(btn, type) {
  const group = btn.closest('.stage-type-toggle');
  group.querySelectorAll('.stage-type-btn').forEach(b => b.className = 'stage-type-btn');
  btn.classList.add('active', type);
  const cond = btn.closest('.stage-editor-item').querySelector('.stage-conditional');
  if (cond) cond.disabled = (type !== 'subagent');
  updateEditorPreview();
}

function renumberStages() {
  document.querySelectorAll('#stage-editor-list .stage-editor-item').forEach((el, i) => {
    const ord = el.querySelector('.stage-editor-order');
    if (ord) ord.textContent = `Stage ${i + 1}`;
  });
}

function updateEditorPreview() {
  const preview = document.getElementById('editor-preview-list');
  if (!preview) return;
  const items = document.querySelectorAll('#stage-editor-list .stage-editor-item');
  if (!items.length) { preview.innerHTML = '<div style="font-size:.8125rem;color:var(--text-3);padding:12px 16px;">No stages defined.</div>'; return; }
  preview.innerHTML = [...items].map((el, i) => {
    const name    = el.querySelector('.stage-name-input')?.value || 'Unnamed Stage';
    const purpose = el.querySelector('.stage-purpose-input')?.value || ' - ';
    const isSubagent = el.querySelector('.stage-type-btn.active.subagent');
    const dot = isSubagent
      ? `<div style="width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0;"></div>`
      : `<div style="width:7px;height:7px;border-radius:50%;background:var(--blue);flex-shrink:0;"></div>`;
    return `<div class="workflow-preview-row">${dot}<span style="font-family:var(--font-mono);font-size:.6875rem;color:var(--text-3);width:16px;">${i+1}</span><div><div style="font-weight:600;font-size:.875rem;">${name}</div><div style="font-size:.75rem;color:var(--text-3);">${purpose}</div></div></div>`;
  }).join('');
}

// ── ExecutionProgress - state machine ─────────────────────────────────────────
function runExecutionProgress(stages, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

  let currentIdx = 0;
  const rows = container.querySelectorAll('.exec-stage-row');
  const timers = container.querySelectorAll('.exec-stage-time');

  function advance() {
    if (currentIdx >= rows.length) return;
    const row = rows[currentIdx];
    const numEl = row.querySelector('.exec-stage-num');
    const timeEl = timers[currentIdx];

    // Set active
    row.classList.remove('waiting');
    row.classList.add('active');
    if (numEl) { numEl.textContent = '⟳'; numEl.className = 'exec-stage-num active'; }

    const duration = 2500 + Math.random() * 2500;
    let elapsed = 0;
    const tick = setInterval(() => {
      elapsed += 100;
      if (timeEl) timeEl.textContent = (elapsed / 1000).toFixed(1) + 's';
    }, 100);

    setTimeout(() => {
      clearInterval(tick);
      row.classList.remove('active');
      row.classList.add('done');
      const isSubagent = row.dataset.type === 'subagent';
      if (numEl) { numEl.textContent = '✓'; numEl.className = isSubagent ? 'exec-stage-num subagent-done' : 'exec-stage-num done'; }
      if (timeEl) { timeEl.classList.add('done'); }
      currentIdx++;
      if (currentIdx < rows.length) {
        setTimeout(advance, 400);
      } else {
        showToast('All stages complete - awaiting delivery', 'success');
      }
    }, duration);
  }

  advance();
}

// ── Chart helpers (called from page scripts) ───────────────────────────────────
function makeLineChart(id, labels, datasets, options = {}) {
  const el = document.getElementById(id);
  if (!el || typeof Chart === 'undefined') return;

  const MONO = '"IBM Plex Mono", monospace';
  const TICK = 'rgba(148,163,184,.45)';

  // Gradient fill: created after layout so chartArea dimensions are correct
  const gradientPlugin = {
    id: 'agGradient',
    beforeDatasetDraw(chart, args) {
      const ds = chart.data.datasets[args.index];
      if (!ds.fill || !ds._gradColor) return;
      const { ctx, chartArea: a } = chart;
      if (!a) return;
      const grad = ctx.createLinearGradient(0, a.top, 0, a.bottom);
      grad.addColorStop(0,   ds._gradColor.replace('__A__', '.22'));
      grad.addColorStop(0.7, ds._gradColor.replace('__A__', '.07'));
      grad.addColorStop(1,   ds._gradColor.replace('__A__', '.00'));
      chart.data.datasets[args.index].backgroundColor = grad;
    }
  };

  // Dot on last data point
  const lastPointPlugin = {
    id: 'agLastDot',
    afterDatasetsDraw(chart) {
      const { ctx } = chart;
      chart.data.datasets.forEach((ds, i) => {
        if (!ds._gradColor) return;
        const meta = chart.getDatasetMeta(i);
        const last = meta.data[meta.data.length - 1];
        if (!last) return;
        const x = last.x, y = last.y;
        const color = ds.borderColor;
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.strokeStyle = 'var(--card, #1E1E1E)';
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
      });
    }
  };

  const processedDatasets = datasets.map(ds => {
    if (ds.fill && ds.borderColor) {
      // Convert hex or rgba borderColor to rgba template
      let gc = ds.borderColor;
      if (gc.startsWith('#')) {
        const r = parseInt(gc.slice(1,3),16), g = parseInt(gc.slice(3,5),16), b = parseInt(gc.slice(5,7),16);
        gc = `rgba(${r},${g},${b},__A__)`;
      } else if (gc.startsWith('rgb(')) {
        gc = gc.replace('rgb(', 'rgba(').replace(')', ',__A__)');
      }
      return { ...ds, _gradColor: gc, backgroundColor: 'transparent' };
    }
    return ds;
  });

  return new Chart(el, {
    type: 'line',
    plugins: [gradientPlugin, lastPointPlugin],
    data: { labels, datasets: processedDatasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(8,8,12,.97)',
          borderColor: 'rgba(255,255,255,.08)',
          borderWidth: 1,
          titleColor: 'rgba(241,245,249,.9)',
          bodyColor: 'rgba(148,163,184,.85)',
          padding: { top: 10, bottom: 10, left: 14, right: 14 },
          cornerRadius: 8,
          titleFont: { family: MONO, size: 10, weight: '700' },
          bodyFont:  { family: MONO, size: 10 },
          displayColors: true,
          boxWidth: 8, boxHeight: 8,
          callbacks: {
            title: (items) => items[0].label,
            label: (ctx) => ` ${ctx.dataset.label}: $${Number(ctx.parsed.y).toLocaleString('en-US', { minimumFractionDigits: 0 })}`
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          border: { display: false },
          ticks: { color: TICK, font: { family: MONO, size: 9 }, maxRotation: 0, padding: 8 }
        },
        y: {
          grid: { display: false },
          border: { display: false },
          position: 'right',
          ticks: {
            color: TICK, font: { family: MONO, size: 9 }, padding: 10, maxTicksLimit: 5,
            callback: (v) => '$' + Number(v).toLocaleString()
          }
        },
        ...options.scales,
      },
      elements: {
        line:  { tension: 0.42, borderWidth: 2 },
        point: { radius: 0, hoverRadius: 4, hoverBorderWidth: 2, hoverBackgroundColor: '#fff' }
      },
      layout: { padding: { top: 8, right: 4 } },
      ...options,
    }
  });
}

function makeBarChart(id, labels, datasets, options = {}) {
  const el = document.getElementById(id);
  if (!el || typeof Chart === 'undefined') return;

  const MONO = '"IBM Plex Mono", monospace';
  const GRID = 'rgba(255,255,255,.045)';
  const TICK = 'rgba(148,163,184,.50)';

  return new Chart(el, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          labels: { color: TICK, font: { family: MONO, size: 10 }, boxWidth: 12, padding: 16 }
        },
        tooltip: {
          backgroundColor: 'rgba(10,10,16,.96)',
          borderColor: 'rgba(255,255,255,.10)',
          borderWidth: 1,
          titleColor: '#f1f5f9',
          bodyColor: 'rgba(148,163,184,.88)',
          padding: 14,
          cornerRadius: 8,
          titleFont: { family: MONO, size: 11 },
          bodyFont:  { family: MONO, size: 11 },
          callbacks: {
            label: (ctx) => ` ${ctx.dataset.label}: $${Number(ctx.parsed.y).toFixed(2)}`
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          border: { display: false },
          ticks: { color: TICK, font: { family: MONO, size: 10 }, maxRotation: 0 }
        },
        y: {
          grid: { display: false },
          border: { display: false },
          ticks: {
            color: TICK, font: { family: MONO, size: 10 },
            callback: (v) => '$' + Number(v).toLocaleString()
          }
        },
      },
      borderRadius: 5,
      borderSkipped: false,
      ...options,
    }
  });
}

function makeDoughnutChart(id, labels, data, colors) {
  const ctx = document.getElementById(id);
  if (!ctx || typeof Chart === 'undefined') return;
  return new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderWidth: 0, hoverOffset: 6 }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'right', labels: { color: '#94A3B8', font: { size: 12 }, padding: 16 } },
        tooltip: {
          backgroundColor: '#161F2C',
          borderColor: '#243041',
          borderWidth: 1,
          titleColor: '#F8FAFC',
          bodyColor: '#94A3B8',
          padding: 12,
        }
      },
      cutout: '65%',
    }
  });
}

// ── Theme Toggle ─────────────────────────────────────────────────────────────
(function initTheme() {
  const root  = document.documentElement;
  const saved = localStorage.getItem('ah_theme') || 'dark';
  root.setAttribute('data-theme', saved);

  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;

    btn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      localStorage.setItem('ah_theme', next);
    });
  });
})();

// ── Entry Gate - required role selection on every page until role is chosen ────
(function initEntryGate() {
  document.addEventListener('DOMContentLoaded', () => {
    const overlay = document.getElementById('entry-gate');
    if (!overlay) return;

    // Entry-gate disabled — was intermittently leaving the page dimmed
    // after the modal closed. Prototype-mode: just let users navigate.
    return;
    // Never show on seller, admin, or demo pages (role already implied by the URL)
    const path = window.location.pathname;
    if (path.startsWith('/admin') || path.startsWith('/seller') || path.startsWith('/demo')) return;

    // Skip if role already chosen
    if (localStorage.getItem('agenthire_role')) return;

    // Show with short delay so page renders first, then lock scroll
    requestAnimationFrame(() => {
      setTimeout(() => {
        overlay.classList.add('open');
        document.body.style.overflow = 'hidden';
      }, 120);
    });

    function choose(role, dest) {
      localStorage.setItem('agenthire_role', role);
      overlay.classList.remove('open');
      document.body.style.overflow = '';
      window.location.href = dest;
    }

    document.getElementById('gate-buyer-btn')
      ?.addEventListener('click', () => choose('buyer', '/marketplace'));
    document.getElementById('gate-seller-btn')
      ?.addEventListener('click', () => choose('seller', '/seller/earnings'));
    document.getElementById('gate-agent-btn')
      ?.addEventListener('click', () => choose('agent', '/sim'));
  });
})();
