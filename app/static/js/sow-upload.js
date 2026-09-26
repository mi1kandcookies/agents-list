/*
 * Statement-of-work upload: drag a file onto a [data-sow-drop] zone (or pick
 * one), send it to POST /api/sow/parse and hand the parsed scope back.
 *
 * Used on /new (flow.js applies the scope to every step), on the home search
 * box (the scope is stashed for /new) and on /jobs/new.
 *
 *   SowUpload.mount(zone, { onParsed: function (scope) {…} })
 *   SowUpload.stash(scope) / SowUpload.take()   // hand a scope to the next page
 *
 * Markup inside the zone (all optional except the file input):
 *   input[type=file][data-sow-input], [data-sow-pick] (button that opens the
 *   picker), [data-sow-status] (live region), [data-sow-bar] (progress fill),
 *   [data-sow-error].
 */
(function () {
  "use strict";

  var MAX_BYTES = 5 * 1024 * 1024;
  var EXTENSIONS = [".pdf", ".docx", ".txt", ".md"];
  var STASH_KEY = "agentslist.sowScope.v1";

  function ext(name) {
    var m = /\.[A-Za-z0-9]+$/.exec(name || "");
    return m ? m[0].toLowerCase() : "";
  }

  function check(file) {
    if (!file) return "Choose a file to upload.";
    if (EXTENSIONS.indexOf(ext(file.name)) < 0) return "Upload a .pdf, .docx, .txt or .md file.";
    if (file.size > MAX_BYTES) return "That file is larger than 5 MB.";
    if (!file.size) return "That file is empty.";
    return null;
  }

  // XHR rather than fetch so the upload can report progress.
  function parse(file, onProgress) {
    return new Promise(function (resolve, reject) {
      var problem = check(file);
      if (problem) { reject(new Error(problem)); return; }
      var data = new FormData();
      data.append("file", file, file.name);
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/sow/parse");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.withCredentials = true;
      xhr.timeout = 60000;
      if (onProgress && xhr.upload) {
        xhr.upload.addEventListener("progress", function (e) {
          if (e.lengthComputable) onProgress(e.loaded / e.total);
        });
        xhr.upload.addEventListener("load", function () { onProgress(1); });
      }
      xhr.addEventListener("load", function () {
        var body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300 && body) { resolve(body); return; }
        if (xhr.status === 404 || xhr.status === 405) { reject(new Error("Uploading a statement of work is not switched on for this server.")); return; }
        if (xhr.status === 429) { reject(new Error("Too many uploads in a row. Wait a minute and try again.")); return; }
        reject(new Error((body && body.error) || "The file could not be read (" + xhr.status + ")."));
      });
      xhr.addEventListener("error", function () { reject(new Error("The upload failed. Check your connection and try again.")); });
      xhr.addEventListener("timeout", function () { reject(new Error("Reading the file took too long. Try again, or paste the text.")); });
      xhr.send(data);
    });
  }

  function hasFiles(e) {
    var types = e.dataTransfer && e.dataTransfer.types;
    if (!types) return false;
    for (var i = 0; i < types.length; i++) if (types[i] === "Files") return true;
    return false;
  }

  /*
   * Wire a zone. ``opts.target`` (default: the zone) is the element that
   * accepts drops; ``opts.onParsed(scope)`` runs on success.
   */
  function mount(zone, opts) {
    opts = opts || {};
    var input = zone.querySelector("[data-sow-input]");
    var pick = zone.querySelector("[data-sow-pick]");
    var status = zone.querySelector("[data-sow-status]");
    var bar = zone.querySelector("[data-sow-bar]");
    var errNode = zone.querySelector("[data-sow-error]");
    var target = opts.target || zone;
    var busy = false;
    var depth = 0;

    function setState(name) {
      zone.dataset.state = name;
      if (pick) pick.disabled = name === "busy";
    }
    function say(text) { if (status) status.textContent = text || ""; }
    function fail(msg) {
      busy = false;
      setState(zone.dataset.done ? "done" : "idle");
      if (errNode) { errNode.textContent = msg; errNode.hidden = false; }
      say("");
      if (opts.onError) opts.onError(msg);
    }

    function handle(file) {
      if (busy || !file) return;
      if (errNode) { errNode.hidden = true; errNode.textContent = ""; }
      var problem = check(file);
      if (problem) { fail(problem); return; }
      busy = true;
      setState("busy");
      if (bar) bar.style.width = "4%";
      say("Uploading " + file.name + "…");
      parse(file, function (p) {
        if (bar) bar.style.width = Math.max(4, Math.round(p * 70)) + "%";
        if (p >= 1) say("Reading " + file.name + "…");
      }).then(function (scope) {
        busy = false;
        if (bar) bar.style.width = "100%";
        zone.dataset.done = "1";
        setState("done");
        say("");
        if (opts.onParsed) opts.onParsed(scope, file);
      }).catch(function (err) { fail(err.message); });
    }

    if (input) {
      input.addEventListener("change", function () {
        var f = input.files && input.files[0];
        input.value = "";
        handle(f);
      });
    }
    if (pick && input) pick.addEventListener("click", function () { input.click(); });

    target.addEventListener("dragenter", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      zone.classList.add("is-over");
    });
    target.addEventListener("dragover", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });
    target.addEventListener("dragleave", function (e) {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) zone.classList.remove("is-over");
    });
    target.addEventListener("drop", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      zone.classList.remove("is-over");
      handle(e.dataTransfer.files && e.dataTransfer.files[0]);
    });
    setState("idle");
    return { handle: handle };
  }

  function stash(scope) {
    try { window.sessionStorage.setItem(STASH_KEY, JSON.stringify(scope)); return true; } catch (e) { return false; }
  }
  function take() {
    try {
      var raw = window.sessionStorage.getItem(STASH_KEY);
      window.sessionStorage.removeItem(STASH_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  window.SowUpload = { mount: mount, parse: parse, check: check, stash: stash, take: take };
})();
