/*
 * "Describe your job" guided flow (/new) and the approve button on
 * /estimate/<engagement_id>.
 *
 * Category defaults and estimate constants come from the server
 * (#flow-config, built in app/intake/estimate.py). estimate() here mirrors
 * estimate() in that module; keep the two in step.
 */
(function () {
  "use strict";

  var STORE_KEY = "agentslist.newJob.v1";
  var TOTAL_STEPS = 9;
  var MAX_MILESTONES = 8;

  // ── Small helpers ──────────────────────────────────────────────────────────
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "html") node.innerHTML = attrs[k];
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return node;
  }
  // Amounts go through the site-wide formatter in money.js ("900 USDC", "12.50 USDC").
  var fmtUSDC = window.fmtUSDC;
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function toCents(d) { return Math.round((Number(d) || 0) * 100); }
  function round5(n) { return Math.round(n / 5) * 5; }
  function isoLocal(d) {
    var z = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return z.toISOString().slice(0, 10);
  }
  function addDays(n) { var d = new Date(); d.setDate(d.getDate() + n); return isoLocal(d); }
  function daysUntil(iso) {
    var today = new Date(isoLocal(new Date()) + "T00:00:00");
    var then = new Date(iso + "T00:00:00");
    return Math.round((then - today) / 86400000);
  }
  function prettyDate(iso) {
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  }
  var uid = (function () { var n = Date.now() % 100000; return function () { n += 1; return "m" + n; }; })();

  var ICONS = {
    up: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M8 3.5 3.5 8l1 1L7.3 6.2V12.5h1.4V6.2L11.5 9l1-1z" fill="currentColor"/></svg>',
    down: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M8 12.5 12.5 8l-1-1-2.8 2.8V3.5H7.3v6.3L4.5 7l-1 1z" fill="currentColor"/></svg>',
    remove: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="m4.2 3.2 3.8 3.8 3.8-3.8 1 1L9 8l3.8 3.8-1 1L8 9l-3.8 3.8-1-1L7 8 3.2 4.2z" fill="currentColor"/></svg>'
  };

  var store = {
    load: function () { try { return JSON.parse(window.localStorage.getItem(STORE_KEY) || "null"); } catch (e) { return null; } },
    save: function (s) { try { window.localStorage.setItem(STORE_KEY, JSON.stringify(s)); } catch (e) { /* private mode */ } },
    clear: function () { try { window.localStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ } }
  };

  // ── API ────────────────────────────────────────────────────────────────────
  var UNAVAILABLE = "Hiring is not switched on for this server yet.";

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 404 || res.status === 405) {
          var err = new Error(UNAVAILABLE); err.unavailable = true; throw err;
        }
        if (!res.ok) {
          throw new Error((data && data.error) || ("Request failed (" + res.status + ")."));
        }
        return data;
      });
    });
  }

  function approvalLocation(approval) {
    var id = approval && (approval.approval_id || approval.id);
    if (!id) throw new Error("The approval could not be started. Please try again.");
    return "/approvals/" + encodeURIComponent(id);
  }

  function hire(engagementId, totalUsdc) {
    return postJSON("/api/engagements/" + encodeURIComponent(engagementId) + "/hire",
      { flow: "web", confirm_amount_usdc: totalUsdc }).then(approvalLocation);
  }

  // `notice` styles the message as a heads-up rather than a failure.
  function showError(node, msg, notice) {
    if (!node) return;
    node.textContent = msg || "";
    node.hidden = !msg;
    node.classList.toggle("is-notice", !!notice);
  }

  // ── /estimate/<id>: approve an existing engagement ────────────────────────
  var estimateBtn = $("#estimate-approve");
  if (estimateBtn) {
    estimateBtn.addEventListener("click", function () {
      var errNode = $("#e-9");
      showError(errNode, "");
      estimateBtn.disabled = true;
      estimateBtn.textContent = "Starting World ID…";
      hire(estimateBtn.dataset.engagementId, Number(estimateBtn.dataset.total))
        .then(function (loc) { window.location.assign(loc); })
        .catch(function (err) {
          showError(errNode, err.unavailable ? err.message + " This engagement is saved; you can approve it here later." : err.message, err.unavailable);
          estimateBtn.disabled = false;
          estimateBtn.textContent = "Approve with World ID";
        });
    });
  }

  var form = $("#flow-form");
  if (!form) return;

  // ── /new: the guided flow ─────────────────────────────────────────────────
  var CONFIG = JSON.parse($("#flow-config").textContent);
  var CATS = CONFIG.categories;
  function cat(key) { return CATS.filter(function (c) { return c.key === key; })[0] || null; }

  /*
   * One-line brief from the free-text outcome. Deterministic for now; this is
   * the hook a scoping agent will replace (it may return a Promise later, so
   * callers go through renderBrief()).
   */
  function briefFromOutcome(text) {
    var t = String(text || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    var m = t.match(/^[\s\S]*?[.!?](?=\s|$)/);
    var first = m ? m[0] : t;
    if (first.length > 140) first = first.slice(0, 137).replace(/\s+\S*$/, "") + "…";
    first = first.charAt(0).toUpperCase() + first.slice(1);
    if (!/[.!?…]$/.test(first)) first += ".";
    return first;
  }

  function guessCategory(text) {
    var t = " " + String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, " ") + " ";
    var best = null, bestScore = 0;
    CATS.forEach(function (c) {
      var score = c.keywords.reduce(function (n, kw) { return n + (t.indexOf(" " + kw + " ") >= 0 ? 1 : 0); }, 0);
      if (score > bestScore) { best = c.key; bestScore = score; }
    });
    return best;
  }

  function freshState(outcome) {
    return {
      v: 1, step: 1, outcome: outcome || "",
      category: null, categoryGuessed: false,
      milestones: [], milestonesFor: null, milestonesEdited: false,
      deadline: { mode: null, date: "" },
      budget: { mode: null, amount: 0 },
      agent: null, engagement: null
    };
  }

  function defaultMilestones(c, budget) {
    return c.milestones.map(function (m) {
      return { id: uid(), title: m.title, amount: round5(budget * m.share), criteria: m.criteria.slice() };
    });
  }

  function msTotal() { return state.milestones.reduce(function (n, m) { return n + (Number(m.amount) || 0); }, 0); }

  // Rescale milestone amounts to `budget`, keeping proportions; the rounding
  // remainder goes on the largest milestone so the sum is exact.
  // Proportions captured when the budget step opens, so dragging the slider
  // down to a tiny value and back up does not lose a milestone's share.
  var shareBase = null;
  function rescale(budget) {
    var ms = state.milestones;
    if (!ms.length) return;
    var base = shareBase && shareBase.length === ms.length ? shareBase
      : ms.map(function (m) { return Number(m.amount) || 0; });
    var sum = base.reduce(function (n, v) { return n + v; }, 0);
    var shares = base.map(function (v) { return sum > 0 ? v / sum : 1 / ms.length; });
    ms.forEach(function (m, i) { m.amount = round5(budget * shares[i]); });
    var diff = budget - msTotal();
    var big = 0;
    ms.forEach(function (m, i) { if (m.amount > ms[big].amount) big = i; });
    ms[big].amount = Math.max(0, ms[big].amount + diff);
  }

  function suggestedSplit() {
    var c = cat(state.category);
    if (!c) return;
    var sum = c.suggested_budget;
    if (state.milestones.length === c.milestones.length) {
      state.milestones.forEach(function (m, i) { m.amount = round5(sum * c.milestones[i].share); });
      var diff = sum - msTotal();
      state.milestones[state.milestones.length - 1].amount += diff;
    } else {
      state.milestones.forEach(function (m) { m.amount = 1; });
      shareBase = null;
      rescale(sum);
    }
    shareBase = state.milestones.map(function (m) { return m.amount; });
  }

  // Mirrors app/intake/estimate.py:estimate().
  function estimate() {
    var c = cat(state.category);
    var per = c ? c.days_per_milestone : 4;
    var total = toCents(msTotal());
    var tips = [], score = 0;
    var counts = state.milestones.map(function (m) { return m.criteria.filter(Boolean).length; });
    if (state.outcome.trim().length >= 80) score += 1; else tips.push("Describe the outcome in a bit more detail.");
    if (counts.length && counts.every(function (n) { return n >= 1; })) score += 1; else tips.push("Give every milestone at least one success criterion.");
    if (counts.length && counts.every(function (n) { return n >= 2; })) score += 1; else tips.push("Add a second success criterion to each milestone.");
    var level = score === 3 ? "good" : score === 2 ? "medium" : "low";
    var buffer = CONFIG.buffer[level];
    var daysLow = Math.max(1, per * state.milestones.length);
    var daysHigh = Math.ceil(+(daysLow * (1 + 2 * buffer)).toFixed(6));
    var fit = null;
    if (state.deadline.date) {
      var left = daysUntil(state.deadline.date);
      fit = left >= daysHigh ? "ok" : left >= daysLow ? "tight" : "short";
    }
    return {
      totalCents: total, costLowCents: total,
      costHighCents: Math.ceil(Math.round(total * (1 + buffer)) / 500) * 500,
      daysLow: daysLow, daysHigh: daysHigh, level: level, tips: tips, fit: fit
    };
  }

  // ── State ──────────────────────────────────────────────────────────────────
  var prefill = (form.dataset.prefill || "").trim();
  var saved = store.load();
  var state;
  if (saved && saved.v === 1 && (!prefill || saved.outcome === prefill)) {
    state = saved;
    if (state.step > 1 || state.outcome) $("#flow-restore").hidden = false;
  } else {
    state = freshState(prefill);
  }
  if (state.step > 8) state.step = 8; // never land on the approve screen from a reload
  // ?agent= (the "Get estimate" link on an agent profile) preselects that agent.
  var picked = JSON.parse($("#flow-agent").textContent || "null");
  if (picked && (!state.agent || state.agent.id !== agentKey(picked))) {
    state.agent = agentChoice(picked);
    if (picked.category_key && state.step <= 2) { state.category = picked.category_key; state.categoryGuessed = false; }
  }
  // The prefill has been applied; drop the query so a reload restores later edits.
  if ((prefill || picked) && window.history.replaceState) {
    try { window.history.replaceState(null, "", window.location.pathname); } catch (e) { /* ignore */ }
  }
  function save() { store.save(state); }

  // ── Rendering ──────────────────────────────────────────────────────────────
  function bind(name, text) { $all('[data-bind="' + name + '"]').forEach(function (n) { n.textContent = text; }); }

  function deadlineText() {
    var d = state.deadline;
    if (d.mode === "asap") return "As soon as possible";
    if (d.mode === "flexible") return "Flexible";
    if (d.date) return prettyDate(d.date);
    return "";
  }

  function renderBrief() {
    var b = briefFromOutcome(state.outcome);
    var node = $("#f-brief");
    node.textContent = b || "Your one-line brief appears here.";
    node.classList.toggle("is-empty", !b);
  }

  function renderSummary() {
    var c = cat(state.category);
    var set = function (key, text, mono) {
      var n = $('[data-sum="' + key + '"]');
      n.textContent = text || "Not set yet";
      n.classList.toggle("is-empty", !text);
      n.classList.toggle("mono", !!(text && mono));
    };
    set("brief", briefFromOutcome(state.outcome));
    set("category", c ? c.label : "");
    set("milestones", state.milestones.length && state.step > 2
      ? plural(state.milestones.length, "milestone") + ", " + fmtUSDC(msTotal()) : "", true);
    set("deadline", deadlineText(), !!state.deadline.date && state.deadline.mode !== "asap" && state.deadline.mode !== "flexible");
    set("budget", state.budget.mode === "suggest" ? "Scoper suggests" :
      state.budget.mode === "custom" ? fmtUSDC(state.budget.amount) : "", state.budget.mode === "custom");
    set("agent", state.agent ? state.agent.name : "");
  }

  function renderProgress() {
    var s = state.step;
    var section = $('.flow-step[data-step="' + s + '"]');
    var name = section.dataset.name;
    $("#flow-count").textContent = s + " of " + TOTAL_STEPS;
    $("#flow-step-name").textContent = name;
    var bar = $(".flow-bar");
    bar.setAttribute("aria-valuenow", s);
    bar.setAttribute("aria-valuetext", "Step " + s + " of " + TOTAL_STEPS + ": " + name);
    $("#flow-bar-fill").style.width = (s / TOTAL_STEPS * 100).toFixed(1) + "%";
  }

  // Step 2
  function renderCategory() {
    if (!state.category) {
      var g = guessCategory(state.outcome);
      if (g) { state.category = g; state.categoryGuessed = true; }
    }
    $all('input[name="category"]').forEach(function (r) { r.checked = r.value === state.category; });
    $("#f-cat-hint").hidden = !(state.category && state.categoryGuessed);
  }

  // Step 3
  function ensureMilestones() {
    var c = cat(state.category);
    if (!c) return;
    if (!state.milestones.length || (state.milestonesFor !== c.key && !state.milestonesEdited)) {
      state.milestones = defaultMilestones(c, c.suggested_budget);
      state.milestonesFor = c.key;
      state.milestonesEdited = false;
      if (state.budget.mode === "custom" && state.budget.amount) rescale(state.budget.amount);
    }
    $("#ms-reset-note").hidden = !(state.milestonesEdited && state.milestonesFor !== c.key);
  }

  function renderMilestones(focusSel) {
    var list = $("#ms-list");
    list.innerHTML = "";
    var n = state.milestones.length;
    state.milestones.forEach(function (m, i) {
      var num = i + 1;
      var titleId = "ms-title-" + m.id, amtId = "ms-amt-" + m.id;
      var title = el("input", { id: titleId, "class": "finput ms-title", type: "text", maxlength: "120", value: m.title,
        "aria-label": "Milestone " + num + " title",
        oninput: function (e) { m.title = e.target.value; state.milestonesEdited = true; save(); renderSummary(); } });
      var amt = el("input", { id: amtId, "class": "finput mono ms-amt-input", type: "number", min: "0", step: "5",
        inputmode: "decimal", value: m.amount || "", "aria-label": "Milestone " + num + " amount in USDC",
        oninput: function (e) {
          m.amount = Math.max(0, Number(e.target.value) || 0);
          state.milestonesEdited = true;
          if (state.budget.mode) state.budget = { mode: "custom", amount: msTotal() };
          $("#ms-total").textContent = fmtUSDC(msTotal(), { unit: false }); save(); renderSummary();
        } });
      function action(kind, label, disabled, fn) {
        return el("button", { type: "button", "class": "icon-btn", "data-act": kind + "-" + m.id,
          "aria-label": label, title: label, disabled: disabled ? "disabled" : null, html: ICONS[kind], onclick: fn });
      }
      list.appendChild(el("li", { "class": "ms" }, [
        el("span", { "class": "ms-num mono", "aria-hidden": "true", text: String(num) }),
        el("div", { "class": "ms-fields" }, [
          title,
          el("div", { "class": "ms-amt" }, [amt, el("span", { "class": "ms-unit mono", "aria-hidden": "true", text: "USDC" })])
        ]),
        el("div", { "class": "ms-actions" }, [
          action("up", "Move milestone " + num + " up", i === 0, function () { move(i, -1, "up"); }),
          action("down", "Move milestone " + num + " down", i === n - 1, function () { move(i, 1, "down"); }),
          action("remove", "Remove milestone " + num, n <= 1, function () { removeMs(i); })
        ])
      ]));
    });
    $("#ms-total").textContent = fmtUSDC(msTotal(), { unit: false });
    $("#ms-add").disabled = n >= MAX_MILESTONES;
    if (focusSel) { var f = $(focusSel); if (f && !f.disabled) f.focus(); else $("#ms-add").focus(); }
  }

  function announce(msg) { var live = $("#flow-live"); live.textContent = ""; setTimeout(function () { live.textContent = msg; }, 30); }

  function move(i, dir, kind) {
    var ms = state.milestones, j = i + dir;
    var tmp = ms[i]; ms[i] = ms[j]; ms[j] = tmp;
    state.milestonesEdited = true; save();
    renderMilestones('[data-act="' + kind + "-" + tmp.id + '"]');
    announce("Moved “" + (tmp.title || "milestone") + "” to position " + (j + 1) + ".");
  }
  function removeMs(i) {
    var gone = state.milestones.splice(i, 1)[0];
    state.milestonesEdited = true;
    if (state.budget.mode === "custom") state.budget.amount = msTotal();
    save();
    var next = state.milestones[Math.min(i, state.milestones.length - 1)];
    renderMilestones(next ? "#ms-title-" + next.id : null);
    renderSummary();
    announce("Removed “" + (gone.title || "milestone") + "”.");
  }
  $("#ms-add").addEventListener("click", function () {
    if (state.milestones.length >= MAX_MILESTONES) return;
    var m = { id: uid(), title: "", amount: 0, criteria: [] };
    state.milestones.push(m);
    state.milestonesEdited = true; save();
    renderMilestones("#ms-title-" + m.id);
  });
  $("#ms-reset").addEventListener("click", function () {
    state.milestonesEdited = false; state.milestones = [];
    ensureMilestones(); save(); renderMilestones(); renderSummary();
    $("#ms-list input").focus();
  });

  // Step 4
  function renderCriteria(focusSel) {
    var wrap = $("#crit-list");
    wrap.innerHTML = "";
    state.milestones.forEach(function (m, i) {
      if (!m.criteria.length) m.criteria.push("");
      var ul = el("ul", { "class": "crit-items" });
      m.criteria.forEach(function (text, k) {
        var id = "crit-" + m.id + "-" + k;
        var input = el("input", { id: id, "class": "finput", type: "text", maxlength: "200", value: text,
          "aria-label": "Milestone " + (i + 1) + ", criterion " + (k + 1),
          placeholder: "e.g. Delivered as a shared doc with sources linked",
          oninput: function (e) { m.criteria[k] = e.target.value; save(); },
          onkeydown: function (e) {
            if (e.key === "Enter") {
              e.preventDefault();
              m.criteria.splice(k + 1, 0, ""); save();
              renderCriteria("#crit-" + m.id + "-" + (k + 1));
            }
          } });
        var rm = el("button", { type: "button", "class": "icon-btn", html: ICONS.remove,
          "aria-label": "Remove criterion " + (k + 1) + " from milestone " + (i + 1),
          onclick: function () {
            m.criteria.splice(k, 1); save();
            renderCriteria(m.criteria.length ? "#crit-" + m.id + "-" + Math.max(0, k - 1) : "#crit-add-" + m.id);
          } });
        ul.appendChild(el("li", { "class": "crit-item" }, [el("span", { "class": "crit-box", "aria-hidden": "true" }), input, rm]));
      });
      wrap.appendChild(el("fieldset", { "class": "crit-group" }, [
        el("legend", {}, [
          el("span", { "class": "mono crit-idx", text: String(i + 1) }),
          el("span", { "class": "crit-title", text: m.title || "Untitled milestone" }),
          el("span", { "class": "mono crit-amt", text: fmtUSDC(m.amount) })
        ]),
        ul,
        el("button", { type: "button", "class": "fbtn fbtn-link", id: "crit-add-" + m.id, text: "Add criterion",
          onclick: function () { m.criteria.push(""); save(); renderCriteria("#crit-" + m.id + "-" + (m.criteria.length - 1)); } })
      ]));
    });
    if (focusSel) { var f = $(focusSel); if (f) f.focus(); }
  }

  // Step 5
  var DEADLINE_DAYS = { week: 7, month: 30 };
  function renderDeadline() {
    $all('input[name="deadline"]').forEach(function (r) { r.checked = r.value === state.deadline.mode; });
    var date = $("#f-date");
    date.min = addDays(0);
    date.value = state.deadline.date || "";
  }
  $("#f-deadline").addEventListener("change", function (e) {
    var mode = e.target.value;
    state.deadline = { mode: mode, date: DEADLINE_DAYS[mode] ? addDays(DEADLINE_DAYS[mode]) : "" };
    renderDeadline(); save(); renderSummary();
  });
  $("#f-date").addEventListener("change", function (e) {
    state.deadline = { mode: e.target.value ? "date" : null, date: e.target.value };
    renderDeadline(); save(); renderSummary();
  });

  // Step 6
  function renderBudget() {
    var mode = state.budget.mode;
    $all('input[name="budget_mode"]').forEach(function (r) { r.checked = r.value === mode; });
    $("#budget-custom").hidden = mode === "suggest";
    $("#budget-suggest-note").hidden = mode !== "suggest";
    var c = cat(state.category);
    $("#budget-cat").textContent = c ? c.label.toLowerCase() : "";
    var amount = state.budget.amount || msTotal();
    var range = $("#f-budget-range");
    range.max = Math.max(10000, Math.ceil(amount * 2 / 50) * 50);
    range.value = amount;
    if (document.activeElement !== $("#f-budget")) $("#f-budget").value = amount;
    var split = $("#budget-split");
    split.innerHTML = "";
    state.milestones.forEach(function (m) {
      split.appendChild(el("li", {}, [el("span", { text: m.title || "Untitled milestone" }), el("span", { "class": "mono", text: fmtUSDC(m.amount) })]));
    });
    split.appendChild(el("li", { "class": "split-total" }, [el("span", { text: "Total" }), el("span", { "class": "mono", text: fmtUSDC(msTotal()) })]));
  }
  function setBudget(v) {
    var amount = Math.max(0, Number(v) || 0);
    state.budget = { mode: "custom", amount: amount };
    if (amount > 0) rescale(amount);
    save(); renderBudget(); renderSummary();
  }
  $("#f-budget-mode").addEventListener("change", function (e) {
    if (e.target.value === "suggest") { state.budget = { mode: "suggest", amount: 0 }; suggestedSplit(); state.budget.amount = msTotal(); }
    else { state.budget = { mode: "custom", amount: msTotal() }; }
    save(); renderBudget(); renderSummary();
  });
  $("#f-budget-range").addEventListener("input", function (e) { setBudget(e.target.value); $("#f-budget").value = e.target.value; });
  $("#f-budget").addEventListener("input", function (e) { setBudget(e.target.value); });

  // Step 7
  var FIT = {
    ok: "Fits your deadline.",
    tight: "Tight: your deadline falls inside this range.",
    "short": "Your deadline is shorter than the lower estimate. Consider fewer milestones or a later date."
  };
  function renderEstimate() {
    var e = estimate();
    bind("est-cost", fmtUSDC(e.costLowCents / 100, { unit: false }) + " to " + fmtUSDC(e.costHighCents / 100, { unit: false }));
    bind("est-cost-note", fmtUSDC(e.totalCents / 100) + " is held in escrow; the upper figure allows for revision work.");
    bind("est-days", e.daysLow === e.daysHigh ? plural(e.daysLow, "day") : e.daysLow + " to " + e.daysHigh + " days");
    bind("est-fit", e.fit ? FIT[e.fit] :
      state.deadline.mode === "asap" ? "You asked for the earliest possible start." : "No fixed deadline.");
    var lvl = $('[data-bind="est-level"]');
    lvl.textContent = e.level.charAt(0).toUpperCase() + e.level.slice(1);
    lvl.className = "conf conf-" + e.level;
    var tips = $('[data-bind="est-tips"]');
    tips.innerHTML = "";
    e.tips.forEach(function (t) { tips.appendChild(el("li", { text: t })); });
  }

  var agentsFor = null;
  function agentList(data) { return Array.isArray(data) ? data : (data && data.agents) || []; }
  function fetchAgents(params) {
    return fetch("/api/agents?" + params, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : []; }).then(agentList).catch(function () { return []; });
  }
  function agentKey(a) { return String(a.agent_id || a.public_id || a.id); }
  function agentChoice(a) { return { id: agentKey(a), name: a.name, card: a }; }
  function priceText(a) {
    if (a.price_hint_usdc != null) return fmtUSDC(a.price_hint_usdc);
    if (a.billing === "per_token") {
      // Token prices are tiny; quote them per million tokens.
      var perM = Number(a.input_price_per_1m) ? Number(a.input_price_per_1m) / 1e6 : (Number(a.current_price) || 0) * 1e6;
      return perM ? fmtUSDC(perM) + " per 1M tokens" : "On request";
    }
    var p = Number(a.current_price) || 0;
    // Per-minute rates are genuinely fractional, so they always keep their cents.
    if (p && a.billing === "per_minute") return fmtUSDC(p, { unit: false, cents: true }) + " USDC per min";
    return p ? fmtUSDC(p) : "On request";
  }
  function loadAgents() {
    var c = cat(state.category);
    if (!c || agentsFor === c.key) return;
    agentsFor = c.key;
    var box = $("#agents"), note = $("#agents-note");
    box.setAttribute("aria-busy", "true");
    $all(".agent, .agents-empty", box).forEach(function (n) { n.remove(); });
    note.textContent = "Finding agents…";
    fetchAgents("category=" + encodeURIComponent(c.agent_category) + "&per_page=3").then(function (list) {
      if (list.length) return { list: list, exact: true };
      return fetchAgents("per_page=3").then(function (all) { return { list: all, exact: false }; });
    }).then(function (res) {
      box.setAttribute("aria-busy", "false");
      var list = res.list.slice(0, 3);
      note.textContent = !list.length ? "" : res.exact
        ? "Listed under " + c.label + ". Pick one to continue."
        : "No agents are listed under " + c.label + " yet. These are other available agents; pick one or browse the marketplace.";
      if (!list.length) {
        box.appendChild(el("div", { "class": "agents-empty" }, [
          el("p", { text: "No agents are available right now." }),
          el("a", { href: "/marketplace", "class": "fbtn fbtn-link", text: "Browse the marketplace" })
        ]));
        return;
      }
      // Keep an agent chosen from its profile visible even outside this category.
      var chosen = state.agent && state.agent.card;
      if (chosen && !list.some(function (a) { return agentKey(a) === state.agent.id; })) list = [chosen].concat(list).slice(0, 3);
      if (state.agent && !list.some(function (a) { return agentKey(a) === state.agent.id; })) state.agent = null;
      list.forEach(function (a) { box.appendChild(agentCard(a)); });
      renderSummary();
    });
  }
  function agentCard(a) {
    var key = agentKey(a);
    var rating = Number(a.rating) > 0 ? Number(a.rating).toFixed(1) : "New";
    var jobs = Number(a.tasks_completed) || 0;
    var input = el("input", { type: "radio", name: "agent", value: key, checked: state.agent && state.agent.id === key ? "checked" : null,
      onchange: function () {
        state.agent = agentChoice(a);
        save(); renderSummary(); showError($("#e-7"), "");
      } });
    return el("label", { "class": "agent" }, [
      input,
      el("span", { "class": "agent-body" }, [
        el("span", { "class": "agent-head" }, [
          el("span", { "class": "agent-name", text: a.name }),
          a.verified ? el("span", { "class": "badge", text: "Verified" }) : null
        ]),
        el("span", { "class": "agent-spec", text: a.description || a.use_case || a.category || "" }),
        el("span", { "class": "agent-stats" }, [
          el("span", {}, [el("span", { "class": "stat-label", text: "Typical price " }), el("span", { "class": "mono", text: priceText(a) })]),
          el("span", {}, [el("span", { "class": "stat-label", text: "Rating " }), el("span", { "class": "mono", text: rating })]),
          el("span", {}, [el("span", { "class": "stat-label", text: "Jobs " }), el("span", { "class": "mono", text: String(jobs) })]),
          a.agent_id || a.public_id ? el("span", { "class": "mono agent-id", text: a.agent_id || a.public_id }) : null
        ])
      ])
    ]);
  }

  // Step 8
  function renderContract() {
    bind("c-brief", briefFromOutcome(state.outcome));
    bind("c-agent", state.agent ? state.agent.name : "");
    bind("c-agent-id", state.agent && /^AGT-/i.test(state.agent.id) ? state.agent.id : "");
    bind("c-deadline", deadlineText() || "Flexible");
    $('[data-bind="c-deadline"]').classList.toggle("mono", !!state.deadline.date && state.deadline.mode !== "asap" && state.deadline.mode !== "flexible");
    bind("c-total", fmtUSDC(msTotal(), { unit: false }));
    var ol = $('[data-bind="c-milestones"]');
    ol.innerHTML = "";
    state.milestones.forEach(function (m) {
      var crit = m.criteria.filter(function (c) { return c.trim(); });
      ol.appendChild(el("li", {}, [
        el("div", { "class": "c-ms-head" }, [el("span", { text: m.title }), el("span", { "class": "mono", text: fmtUSDC(m.amount) })]),
        crit.length ? el("ul", { "class": "c-ms-criteria" }, crit.map(function (c) { return el("li", { text: c }); })) : null
      ]));
    });
    renderSow();
  }
  function renderSow() {
    var e = state.engagement;
    var box = $("#sow-created");
    if (!e) { box.hidden = true; return; }
    box.hidden = false;
    $("#sow-eng").textContent = e.id;
    $("#sow-hash").textContent = e.sow_hash || "";
    var dd = $('[data-bind="c-sow"]');
    dd.innerHTML = "";
    dd.appendChild(el("span", { "class": "mono hash", text: e.sow_hash || "" }));
  }

  // ── Validation ─────────────────────────────────────────────────────────────
  function validate(step) {
    var err = null, focus = null;
    if (step === 1) {
      if (state.outcome.trim().length < 10) { err = "Tell us a little more about the result you want (at least a sentence)."; focus = "#f-outcome"; }
    } else if (step === 2) {
      if (!state.category) { err = "Choose the kind of work."; focus = 'input[name="category"]'; }
    } else if (step === 3) {
      var bad = state.milestones.filter(function (m) { return !m.title.trim(); })[0];
      var zero = state.milestones.filter(function (m) { return !(Number(m.amount) > 0); })[0];
      if (!state.milestones.length) { err = "Add at least one milestone."; focus = "#ms-add"; }
      else if (bad) { err = "Every milestone needs a title."; focus = "#ms-title-" + bad.id; }
      else if (zero) { err = "Every milestone needs an amount above zero."; focus = "#ms-amt-" + zero.id; }
    } else if (step === 4) {
      var empty = state.milestones.filter(function (m) { return !m.criteria.some(function (c) { return c.trim(); }); })[0];
      if (empty) { err = "Add at least one success criterion to “" + empty.title + "”."; focus = "#crit-" + empty.id + "-0"; }
    } else if (step === 5) {
      if (!state.deadline.mode) { err = "Choose a timing option or pick a date."; focus = 'input[name="deadline"]'; }
      else if (state.deadline.date && daysUntil(state.deadline.date) < 0) { err = "Pick a date from today onwards."; focus = "#f-date"; }
    } else if (step === 6) {
      if (!state.budget.mode) { err = "Set a budget or let the scoper suggest one."; focus = 'input[name="budget_mode"]'; }
      else if (msTotal() < 10) { err = "The budget needs to be at least 10 USDC."; focus = "#f-budget"; }
      else if (state.milestones.some(function (m) { return !(Number(m.amount) > 0); })) {
        err = "That budget is too small to split across " + state.milestones.length + " milestones. Raise it or remove a milestone.";
        focus = "#f-budget";
      }
    } else if (step === 7) {
      if (!state.agent) { err = "Pick an agent to continue."; focus = 'input[name="agent"]'; }
    }
    showError($("#e-" + step), err);
    if (err && focus) { var f = $(focus); if (f) f.focus(); }
    return !err;
  }

  // ── Navigation ─────────────────────────────────────────────────────────────
  function enter(step) {
    if (step === 2) renderCategory();
    if (step === 3) { ensureMilestones(); renderMilestones(); }
    if (step === 4) renderCriteria();
    if (step === 5) renderDeadline();
    if (step === 6) {
      shareBase = state.milestones.map(function (m) { return Number(m.amount) || 0; });
      if (!state.budget.mode) state.budget = { mode: "custom", amount: msTotal() };
      renderBudget();
    }
    if (step === 7) { renderEstimate(); loadAgents(); }
    if (step === 8 || step === 9) renderContract();
  }

  function show(step, moveFocus) {
    // Going back to edit invalidates a statement of work created earlier.
    if (step < 8 && state.engagement) state.engagement = null;
    state.step = step;
    $all(".flow-step").forEach(function (s) { s.hidden = Number(s.dataset.step) !== step; });
    enter(step);
    renderProgress();
    renderSummary();
    $("#flow-back").hidden = step === 1;
    var next = $("#flow-next");
    next.hidden = step === TOTAL_STEPS;
    next.textContent = step === 8 ? "Looks right" : "Continue";
    save();
    if (moveFocus) {
      var h = $('.flow-step[data-step="' + step + '"] h2');
      if (h) h.focus();
      window.scrollTo({ top: Math.max(0, $("#flow").offsetTop - 16), behavior: "smooth" });
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!validate(state.step)) return;
    if (state.step < TOTAL_STEPS) show(state.step + 1, true);
  });
  $("#flow-back").addEventListener("click", function () { if (state.step > 1) show(state.step - 1, true); });

  // Step 1 inputs
  var outcomeEl = $("#f-outcome");
  outcomeEl.value = state.outcome;
  outcomeEl.addEventListener("input", function () {
    state.outcome = outcomeEl.value;
    if (state.categoryGuessed || !state.category) {
      var g = guessCategory(state.outcome);
      state.category = g; state.categoryGuessed = !!g;
    }
    renderBrief(); renderSummary(); save();
  });
  outcomeEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit")); }
  });

  $("#f-category").addEventListener("change", function (e) {
    state.category = e.target.value; state.categoryGuessed = false;
    agentsFor = null;
    $("#f-cat-hint").hidden = true;
    showError($("#e-2"), "");
    save(); renderSummary();
  });

  $("#flow-reset").addEventListener("click", function () {
    store.clear();
    state = freshState("");
    agentsFor = null;
    outcomeEl.value = "";
    $("#flow-restore").hidden = true;
    renderBrief();
    show(1, false);
    outcomeEl.focus();
  });

  // Step 9: create the engagement, then start the World ID approval.
  $("#approve").addEventListener("click", function () {
    var btn = this, errNode = $("#e-9");
    showError(errNode, "");
    btn.disabled = true;
    btn.textContent = "Creating statement of work…";
    var total = msTotal();
    var created = state.engagement ? Promise.resolve(state.engagement) :
      postJSON("/api/engagements", {
        agent_id: state.agent.id,
        outcome: state.outcome.trim(),
        brief: briefFromOutcome(state.outcome),
        category: state.category,
        budget_usdc: total,
        deadline: state.deadline.date || null,
        milestones: state.milestones.map(function (m) {
          return { title: m.title.trim(), amount_usdc: Number(m.amount),
            acceptance: m.criteria.filter(function (c) { return c.trim(); }).map(function (c) { return "- " + c.trim(); }).join("\n") };
        })
      }).then(function (eng) {
        state.engagement = { id: eng.engagement_id || eng.id, sow_hash: eng.sow_hash || "" };
        save(); renderSow();
        return state.engagement;
      });
    created.then(function (eng) {
      btn.textContent = "Starting World ID…";
      return hire(eng.id, total);
    }).then(function (loc) {
      store.clear();
      window.location.assign(loc);
    }).catch(function (err) {
      var msg = err.message;
      if (err.unavailable) {
        msg += state.engagement
          ? " Your statement of work is saved as " + state.engagement.id + "; you can approve it later."
          : " Your answers are saved on this device, so you can come back and approve later.";
      }
      showError(errNode, msg, err.unavailable);
      btn.disabled = false;
      btn.textContent = "Approve with World ID";
    });
  });

  // ── Boot ───────────────────────────────────────────────────────────────────
  renderBrief();
  show(state.step, false);
})();
