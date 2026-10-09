/* Selection UI for gh-deploy-watcher. No framework, no build step.
 * All server-supplied text reaches the page through textContent or
 * setAttribute only (see el() below). */
(function () {
  "use strict";

  var SEP = "\u0000";
  var root = typeof window !== "undefined" ? window : {}; // root.ghdwDirty is window.ghdwDirty

  // ---------------------------------------------------------------------
  // Pure helpers: no DOM, no network. A "selection" is an array of
  // {repo, file, env, label}; a config is {repos: [{repo, workflows: [...]}]}.
  // ---------------------------------------------------------------------

  function itemKey(item) { return item.repo + SEP + item.file; }

  function flatten(config) {
    var out = [];
    ((config && config.repos) || []).forEach(function (r) {
      r.workflows.forEach(function (w) {
        out.push({ repo: r.repo, file: w.file, env: w.env, label: w.label });
      });
    });
    return out;
  }

  function indexItems(items) {
    var map = new Map();
    items.forEach(function (it) { map.set(itemKey(it), it); });
    return map;
  }

  /** What changed between the loaded config and the current selection. */
  function diffSelection(loaded, selected) {
    var before = indexItems(loaded), after = indexItems(selected);
    var diff = { added: [], removed: [], edited: [] };
    after.forEach(function (item, key) {
      var old = before.get(key);
      if (!old) { diff.added.push(item); }
      else if (old.env !== item.env || old.label !== item.label) { diff.edited.push(item); }
    });
    before.forEach(function (item, key) {
      if (!after.has(key)) { diff.removed.push(item); }
    });
    return diff;
  }

  function hasChanges(diff) {
    return diff.added.length + diff.removed.length + diff.edited.length > 0;
  }

  /** Body for PUT /api/config: loaded order first, new entries after. */
  function buildPutBody(loadedConfig, selected, baseHash) {
    var position = new Map();
    flatten(loadedConfig).forEach(function (it, i) { position.set(itemKey(it), i); });
    var ordered = selected.map(function (it, i) { return { it: it, i: i }; }).sort(function (a, b) {
      var pa = position.has(itemKey(a.it)) ? position.get(itemKey(a.it)) : Infinity;
      var pb = position.has(itemKey(b.it)) ? position.get(itemKey(b.it)) : Infinity;
      return pa === pb ? a.i - b.i : (pa < pb ? -1 : 1);
    });
    var byRepo = new Map();
    ((loadedConfig && loadedConfig.repos) || []).forEach(function (r) { byRepo.set(r.repo, []); });
    ordered.forEach(function (o) {
      if (!byRepo.has(o.it.repo)) { byRepo.set(o.it.repo, []); }
      byRepo.get(o.it.repo).push({ file: o.it.file, env: o.it.env, label: o.it.label });
    });
    var repos = [];
    byRepo.forEach(function (workflows, repo) {
      if (workflows.length) { repos.push({ repo: repo, workflows: workflows }); }
    });
    return { base_hash: baseHash, repos: repos };
  }

  function countTracked(onMap) {
    var repos = 0, workflows = 0;
    onMap.forEach(function (files) {
      if (files.size) { repos += 1; workflows += files.size; }
    });
    return { repos: repos, workflows: workflows };
  }

  /** Split "org/name" for display (first slash only). */
  function splitRepo(display) {
    var i = display.indexOf("/");
    return i < 0 ? ["", display] : [display.slice(0, i + 1), display.slice(i + 1)];
  }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------

  var state = {
    token: "",
    repos: [],              // [{name, display}]
    reposStatus: "loading", // loading | ok | error
    loadedConfig: { repos: [] },
    baseHash: "",
    drafts: new Map(),      // repo -> Map(file -> {env, label}); survives toggling off
    on: new Map(),          // repo -> Set(file) currently selected
    workflows: new Map(),   // repo -> {status, list, error}
    current: null,
    query: "",
    saved: false,
    busy: false
  };

  root.ghdwDirty = false;

  function mapFor(map, key, make) {
    if (!map.has(key)) { map.set(key, make()); }
    return map.get(key);
  }
  function draftsFor(repo) { return mapFor(state.drafts, repo, function () { return new Map(); }); }
  function onFor(repo) { return mapFor(state.on, repo, function () { return new Set(); }); }

  function selectedItems() {
    var out = [];
    state.on.forEach(function (files, repo) {
      files.forEach(function (file) {
        var d = draftsFor(repo).get(file);
        if (d) { out.push({ repo: repo, file: file, env: d.env, label: d.label }); }
      });
    });
    return out;
  }

  function resetFromConfig(config) {
    state.loadedConfig = config;
    state.drafts = new Map();
    state.on = new Map();
    flatten(config).forEach(function (it) {
      draftsFor(it.repo).set(it.file, { env: it.env, label: it.label });
      onFor(it.repo).add(it.file);
    });
  }

  function hasLoadedWorkflows(repo) {
    return flatten(state.loadedConfig).some(function (it) { return it.repo === repo; });
  }

  // ---------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------

  function apiError(status, kind, message) {
    var err = new Error(message);
    err.status = status;
    err.kind = kind;
    return err;
  }

  function api(method, path, body) {
    var opts = { method: method, headers: { "X-Token": state.token } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (res.ok) { return data; }
        var e = (data && data.error) || {};
        throw apiError(res.status, e.kind || "http", e.message || ("HTTP " + res.status));
      });
    }, function () {
      throw apiError(0, "network", "Cannot reach the local server.");
    });
  }

  function workflowsPath(repo) { return "/api/repos/" + encodeURIComponent(repo) + "/workflows"; }

  // ---------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function clear(node) { node.textContent = ""; }

  /** Create an element. props: class, text, on {event: fn}, anything else is an attribute. */
  function el(tag, props, kids) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (k === "class") { node.className = v; }
      else if (k === "text") { node.textContent = v; }
      else if (k === "on") {
        Object.keys(v).forEach(function (ev) { node.addEventListener(ev, v[ev]); });
      } else if (v === true) { node.setAttribute(k, ""); }
      else if (v !== false && v != null) { node.setAttribute(k, String(v)); }
    });
    (kids || []).forEach(function (c) {
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function progress(text) {
    return el("div", { class: "progress", role: "status" }, [el("div", { text: text }), el("div", { class: "bar" })]);
  }

  function repoDisplay(name) {
    for (var i = 0; i < state.repos.length; i++) {
      if (state.repos[i].name === name) { return state.repos[i].display; }
    }
    return name;
  }

  // ---------------------------------------------------------------------
  // Notices
  // ---------------------------------------------------------------------

  function clearNotice() { clear($("notice")); }

  function showNotice(kind, message, extra, action) {
    var box = el("div", { class: "notice " + kind, role: kind === "error" ? "alert" : "status" });
    box.appendChild(el("p", { text: message }));
    if (extra) { box.appendChild(extra); }
    if (action) { box.appendChild(el("button", { type: "button", class: "ghost", text: action.label, on: { click: action.run } })); }
    var slot = $("notice");
    clear(slot);
    slot.appendChild(box);
  }

  function endSession(kind, message) {
    document.body.classList.add("over");
    root.ghdwDirty = false;
    showNotice(kind, message);
  }

  function showInvalidSession() {
    endSession("error", "Invalid session. Start the selection UI again from gh-deploy-watcher.");
  }

  function showSignedOut() {
    showNotice("error", "You are not signed in to GitHub. Run this in a terminal, then retry:",
      el("code", { text: "gh auth login" }), { label: "Retry", run: loadAll });
  }

  /** One place that turns an API failure into the right banner. */
  function showFailure(err, retry) {
    if (err.status === 401) { showInvalidSession(); }
    else if (err.kind === "auth") { showSignedOut(); }
    else if (err.status === 409) {
      showNotice("error", "The config changed on disk. Reload to see the latest, then redo your edits.",
        null, { label: "Reload", run: loadAll });
    } else if (err.status === 422) { showNotice("error", err.message); }
    else {
      showNotice("error", err.message || "Something went wrong.", null,
        retry ? { label: "Retry", run: retry } : null);
    }
  }

  // ---------------------------------------------------------------------
  // Rendering: header, summary, repo list
  // ---------------------------------------------------------------------

  function renderCounters() {
    var c = countTracked(state.on);
    $("st-repos").textContent = String(c.repos);
    $("st-wf").textContent = String(c.workflows);
  }

  function updateButtons(changed) {
    $("save").disabled = !changed || state.busy;
    $("done").hidden = !state.saved;
    $("done").disabled = changed || state.busy;
    $("cancel").disabled = state.busy;
  }

  function summaryNodes(diff) {
    var parts = [];
    if (diff.added.length) { parts.push(el("b", { class: "plus", text: "+" + diff.added.length + " added" })); }
    if (diff.removed.length) { parts.push(el("b", { class: "minus", text: "−" + diff.removed.length + " removed" })); }
    if (diff.edited.length) { parts.push(el("b", { text: diff.edited.length + " edited" })); }
    var out = ["Changes: "];
    parts.forEach(function (p, i) { if (i) { out.push(" · "); } out.push(p); });
    return out;
  }

  function renderSummary() {
    var diff = diffSelection(flatten(state.loadedConfig), selectedItems());
    var changed = hasChanges(diff);
    root.ghdwDirty = changed;
    var box = $("summary");
    clear(box);
    if (changed) { summaryNodes(diff).forEach(function (n) { box.appendChild(typeof n === "string" ? document.createTextNode(n) : n); }); }
    else { box.textContent = state.saved ? "Saved" : "No changes yet"; }
    updateButtons(changed);
  }

  function repoButton(repo) {
    var parts = splitRepo(repo.display);
    var count = onFor(repo.name).size;
    var label = el("span", { class: "label" }, [el("bdi", null, [
      el("span", { class: "org", text: parts[0] }), el("span", { class: "name", text: parts[1] })])]);
    var kids = [label];
    if (count) { kids.push(el("span", { class: "badge", text: String(count) })); }
    var btn = el("button", { type: "button", class: "repo" + (repo.name === state.current ? " active" : ""),
      "data-repo": repo.name, on: { click: function () { selectRepo(repo.name); } } }, kids);
    if (repo.name === state.current) { btn.setAttribute("aria-current", "true"); }
    return btn;
  }

  function matchingRepos() {
    var q = state.query.trim().toLowerCase();
    return state.repos.filter(function (r) { return r.display.toLowerCase().indexOf(q) >= 0; });
  }

  function repoListNodes() {
    var list = matchingRepos();
    var tracked = list.filter(function (r) { return onFor(r.name).size > 0; });
    var rest = list.filter(function (r) { return onFor(r.name).size === 0; });
    var nodes = [];
    if (tracked.length) { nodes.push(el("div", { class: "group", text: "Tracked" })); }
    tracked.forEach(function (r) { nodes.push(repoButton(r)); });
    if (rest.length) { nodes.push(el("div", { class: "group", text: tracked.length ? "All repos" : "Repos" })); }
    rest.forEach(function (r) { nodes.push(repoButton(r)); });
    if (!list.length) { nodes.push(el("div", { class: "empty", text: state.query ? "No repos match your search." : "No repos found." })); }
    return nodes;
  }

  function renderRepos() {
    var box = $("repos");
    var focused = document.activeElement && box.contains(document.activeElement)
      ? document.activeElement.getAttribute("data-repo") : null;
    clear(box);
    if (state.reposStatus === "loading") { box.appendChild(progress("Loading repos")); }
    else if (state.reposStatus === "error") {
      box.appendChild(el("div", { class: "empty" }, [el("div", { text: "Could not load repos." }),
        el("button", { type: "button", class: "ghost", text: "Retry", on: { click: loadAll } })]));
    } else { repoListNodes().forEach(function (n) { box.appendChild(n); }); }
    $("count").textContent = state.reposStatus === "ok" ? String(state.repos.length) : "";
    if (focused !== null) { focusRepoButton(focused); }
  }

  function focusRepoButton(name) {
    var buttons = $("repos").querySelectorAll("button.repo");
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].getAttribute("data-repo") === name) { buttons[i].focus(); return; }
    }
  }

  function markActiveRepo() {
    var buttons = $("repos").querySelectorAll("button.repo");
    for (var i = 0; i < buttons.length; i++) {
      var active = buttons[i].getAttribute("data-repo") === state.current;
      buttons[i].classList.toggle("active", active);
      if (active) { buttons[i].setAttribute("aria-current", "true"); }
      else { buttons[i].removeAttribute("aria-current"); }
    }
  }

  /** After a toggle: counts and grouping change, entry edits only change the summary. */
  function selectionChanged() {
    renderCounters();
    renderRepos();
    renderSummary();
  }

  // ---------------------------------------------------------------------
  // Rendering: the workflows panel
  // ---------------------------------------------------------------------

  /** Rows = workflows GitHub returned, plus tracked files GitHub no longer lists. */
  function rowModels(repo, list) {
    var seen = new Set(list.map(function (w) { return w.file; }));
    var rows = list.slice();
    draftsFor(repo).forEach(function (entry, file) {
      if (onFor(repo).has(file) && !seen.has(file)) {
        rows.push({ file: file, name: file, state: "not found", suggested: entry, preselected: false, tracked: entry });
      }
    });
    return rows;
  }

  function entryFor(repo, w) {
    var drafts = draftsFor(repo);
    if (!drafts.has(w.file)) {
      var base = w.tracked || w.suggested;
      drafts.set(w.file, { env: base.env, label: base.label });
    }
    return drafts.get(w.file);
  }

  function segButton(entry, value, onPick) {
    var btn = el("button", { type: "button", "data-v": value, text: value, on: { click: function () { onPick(value); } } });
    return btn;
  }

  function buildRow(repo, w) {
    var entry = entryFor(repo, w);
    var files = onFor(repo);
    var sw = el("input", { type: "checkbox", role: "switch", class: "switch", "aria-label": "Track " + w.name });
    var label = el("input", { type: "text", class: "label-in", "aria-label": "Label for " + w.name, autocomplete: "off" });
    var seg = el("div", { class: "seg", role: "group", "aria-label": "Environment for " + w.name });
    var row = el("div", { class: "row" }, [sw,
      el("div", { class: "file" }, [el("bdi", { text: w.name }), el("small", { text: w.file })]), label, seg]);
    if (w.state && w.state !== "active") {
      row.classList.add("dim");
      row.querySelector(".file").insertBefore(el("span", { class: "tag", text: w.state }), row.querySelector(".file small"));
    }
    var buttons = {};
    ["prd", "dev"].forEach(function (v) {
      buttons[v] = segButton(entry, v, function (picked) { entry.env = picked; paint(); renderSummary(); });
      seg.appendChild(buttons[v]);
    });
    function paint() {
      var on = files.has(w.file);
      sw.checked = on;
      label.value = entry.label;
      label.disabled = !on;
      row.classList.toggle("off", !on);
      ["prd", "dev"].forEach(function (v) {
        buttons[v].disabled = !on;
        buttons[v].classList.toggle("on", entry.env === v);
        buttons[v].setAttribute("aria-pressed", entry.env === v ? "true" : "false");
      });
    }
    sw.addEventListener("change", function () {
      if (sw.checked) { files.add(w.file); } else { files.delete(w.file); }
      paint();
      selectionChanged();
    });
    label.addEventListener("input", function () { entry.label = label.value; renderSummary(); });
    paint();
    return row;
  }

  function detailHead(repo, ready) {
    var title = el("div", null, [el("h3", null, [el("bdi", { text: repoDisplay(repo) })])]);
    var btn = el("button", { type: "button", class: "ghost", id: "suggested", text: "Select suggested",
      on: { click: applySuggested } });
    btn.disabled = !ready;
    return el("div", { class: "head" }, [title, btn]);
  }

  function renderDetail() {
    var box = $("detail");
    clear(box);
    var repo = state.current;
    if (!repo) { box.appendChild(el("div", { class: "empty", text: "Select a repo to choose its deploy workflows." })); return; }
    var wf = state.workflows.get(repo);
    var ready = !!wf && wf.status === "ok";
    box.appendChild(detailHead(repo, ready));
    if (!wf || wf.status === "loading") { box.appendChild(progress("Loading workflows")); return; }
    if (wf.status === "error") { box.appendChild(workflowError(repo, wf.error)); return; }
    if (!hasLoadedWorkflows(repo)) {
      box.appendChild(el("div", { class: "hint", text: "Workflows with “deploy” in the name are suggested. Nothing is saved until you press Save." }));
    }
    var rows = el("div", { class: "rows" });
    rowModels(repo, wf.list).forEach(function (w) { rows.appendChild(buildRow(repo, w)); });
    if (!rows.firstChild) { rows.appendChild(el("div", { class: "empty", text: "This repo has no workflows." })); }
    box.appendChild(rows);
    box.appendChild(el("div", { class: "legend", text: "Label is what shows in the menu bar dropdown. Env drives the PRD/DEV filter and the re-run confirmation." }));
  }

  function workflowError(repo, err) {
    if (err.kind === "auth") {
      return el("div", { class: "inline-note", role: "alert" }, [
        el("p", { text: "You are not signed in to GitHub. Run this in a terminal, then retry:" }),
        el("code", { text: "gh auth login" }),
        el("button", { type: "button", class: "ghost", text: "Retry", on: { click: function () { retryWorkflows(repo); } } })]);
    }
    return el("div", { class: "inline-note", role: "alert" }, [
      el("p", { text: "Could not load workflows for this repo. " + (err.message || "") }),
      el("button", { type: "button", class: "ghost", text: "Retry", on: { click: function () { retryWorkflows(repo); } } })]);
  }

  // ---------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------

  function applyPreselect(repo, list) {
    var files = onFor(repo);
    list.forEach(function (w) {
      if (!w.preselected) { return; }
      draftsFor(repo).set(w.file, { env: w.suggested.env, label: w.suggested.label });
      files.add(w.file);
    });
  }

  function applySuggested() {
    var repo = state.current, wf = repo && state.workflows.get(repo);
    if (!wf || wf.status !== "ok") { return; }
    var files = onFor(repo);
    files.clear();
    wf.list.forEach(function (w) {
      draftsFor(repo).set(w.file, { env: w.suggested.env, label: w.suggested.label });
      if (w.preselected) { files.add(w.file); }
    });
    renderDetail();
    selectionChanged();
  }

  function ensureWorkflows(repo) {
    var known = state.workflows.get(repo);
    if (known && known.status !== "error") { return; }
    state.workflows.set(repo, { status: "loading", list: [], error: null });
    api("GET", workflowsPath(repo)).then(function (list) {
      state.workflows.set(repo, { status: "ok", list: list, error: null });
      if (!hasLoadedWorkflows(repo) && onFor(repo).size === 0) { applyPreselect(repo, list); }
    }, function (err) {
      state.workflows.set(repo, { status: "error", list: [], error: err });
      if (err.status === 401) { showInvalidSession(); }
    }).then(function () {
      if (state.current === repo) { renderDetail(); }
      selectionChanged();
    });
  }

  function retryWorkflows(repo) {
    state.workflows.delete(repo);
    ensureWorkflows(repo);
    if (state.current === repo) { renderDetail(); }
  }

  function selectRepo(name) {
    state.current = name;
    markActiveRepo();
    renderDetail();
    ensureWorkflows(name);
    renderDetail();
  }

  function applySession(data) {
    $("login").textContent = data.login;
    state.baseHash = data.config_hash;
    resetFromConfig(data.config);
    state.workflows = new Map();
    state.saved = false;
  }

  function loadAll() {
    clearNotice();
    state.reposStatus = "loading";
    renderRepos();
    api("GET", "/api/session").then(function (data) {
      applySession(data);
      return api("GET", "/api/repos");
    }).then(function (list) {
      state.repos = list;
      state.reposStatus = "ok";
      if (state.current && !list.some(function (r) { return r.name === state.current; })) { state.current = null; }
      selectionChanged();
      renderDetail();
      if (state.current) { ensureWorkflows(state.current); renderDetail(); }
    }).catch(function (err) {
      state.reposStatus = "error";
      renderRepos();
      showFailure(err, loadAll);
    });
  }

  function save() {
    if (state.busy) { return; }
    var body = buildPutBody(state.loadedConfig, selectedItems(), state.baseHash);
    state.busy = true;
    clearNotice();
    renderSummary();
    api("PUT", "/api/config", body).then(function (res) {
      state.loadedConfig = { repos: body.repos };
      state.baseHash = res.config_hash;
      state.saved = true;
    }, function (err) {
      showFailure(err, null);
    }).then(function () {
      state.busy = false;
      renderSummary();
    });
  }

  function finish(message) {
    if (state.busy) { return; }
    state.busy = true;
    updateButtons(false);
    api("POST", "/api/done", {}).then(null, function () { /* the server may already be gone */ }).then(function () {
      endSession("info", message);
    });
  }

  function bindControls() {
    $("q").addEventListener("input", function () { state.query = $("q").value; renderRepos(); });
    $("save").addEventListener("click", save);
    $("cancel").addEventListener("click", function () { finish("Nothing was saved. You can close this window."); });
    $("done").addEventListener("click", function () { finish("All set. You can close this window."); });
  }

  /** The token travels in the URL fragment only; drop it from the address at once. */
  function takeToken() {
    var raw = (location.hash || "").replace(/^#/, "");
    var token = raw;
    try { token = decodeURIComponent(raw); } catch (e) { token = raw; }
    if (location.hash) { history.replaceState(null, "", location.pathname + location.search); }
    return token;
  }

  function start() {
    state.token = takeToken();
    if (!state.token) { showInvalidSession(); return; }
    bindControls();
    renderDetail();
    renderSummary();
    loadAll();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); }
    else { start(); }
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { diffSelection: diffSelection, buildPutBody: buildPutBody, flatten: flatten,
      hasChanges: hasChanges, countTracked: countTracked, splitRepo: splitRepo };
  }
})();
