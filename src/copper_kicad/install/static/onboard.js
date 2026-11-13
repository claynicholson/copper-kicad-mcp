(function () {
  "use strict";
  var TOKEN = new URLSearchParams(location.search).get("t") || "";
  var S = null;                 // last state from the server
  var cur = 0;                  // index into STEPS
  var picks = {};               // import item id -> bool (the user's ticks)
  var appPicks = null;          // client id -> bool
  var notes = {};               // step -> {cls, title?, text, fix?}
  var busy = {};                // step -> false | true | "progress message"
  var lines = {};               // step -> recent job lines
  var hidden = false;
  var tested = {};              // client id -> launch-test detail (this page session)
  var imported = false;         // the import step was applied (or skipped) in this session
  var testResult = null;        // {detail, reply}
  var ghCode = null;
  var closed = false;

  var STEPS = [
    { id: "s-kicad", key: "kicad", label: "KiCad" },
    { id: "s-import", key: "import", label: "Your KiCad setup" },
    { id: "s-apps", key: "apps", label: "AI apps" },
    { id: "s-github", key: "github", label: "GitHub" },
    { id: "s-router", key: "router", label: "Autorouter" },
    { id: "s-test", key: "test", label: "Try it" },
    { id: "s-done", key: "done", label: "Ready" }
  ];
  var LAST = STEPS.length - 2;  // the "Ready" screen is not counted as a step

  // ── tiny DOM helper: text is always set as text, never as HTML ─────────
  function h(tag, attrs) {
    var e = document.createElement(tag), i, k;
    attrs = attrs || {};
    for (k in attrs) {
      if (k === "class") e.className = attrs[k];
      else if (k === "on") { for (i in attrs.on) e.addEventListener(i, attrs.on[i]); }
      else if (attrs[k] === true) e.setAttribute(k, "");
      else if (attrs[k] !== false && attrs[k] != null) e.setAttribute(k, attrs[k]);
    }
    for (i = 2; i < arguments.length; i++) add(e, arguments[i]);
    return e;
  }
  function add(e, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { add(e, x); }); return; }
    e.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  // `like this` in server text becomes <code>, still as text nodes
  function rich(text) {
    return String(text == null ? "" : text).split("`").map(function (part, i) { return i % 2 ? h("code", {}, part) : part; });
  }
  function $(id) { return document.getElementById(id); }
  function fill(id, nodes) { var el = $(id); el.textContent = ""; add(el, nodes); }
  var SVGNS = "http://www.w3.org/2000/svg";
  function tick() {                       // a drawn check mark (the only status icon besides "!")
    var s = document.createElementNS(SVGNS, "svg"), p = document.createElementNS(SVGNS, "path");
    s.setAttribute("viewBox", "0 0 16 16"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    p.setAttribute("d", "M3 8.5 6.5 12 13 4.5"); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "2"); p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.appendChild(p); return s;
  }
  function dot(kind, ch) { return h("span", { class: "dot " + (kind || ""), "aria-hidden": "true" }, kind === "ok" ? tick() : (ch || "")); }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
  function noteEl(n) {
    if (!n) return null;
    return h("div", { class: "note " + (n.cls || ""), role: n.cls === "bad" ? "alert" : "status" },
      n.title ? h("span", { class: "t" }, rich(n.title)) : null, rich(cap(n.text || "")), n.fix ? h("span", { class: "fix" }, n.fix) : null);
  }
  function working(text, key) {
    return [h("div", { class: "working", role: "status" }, h("span", { class: "spin", "aria-hidden": "true" }), text || "Working…"),
            (lines[key] || []).length > 1 ? h("div", { class: "lines", "aria-hidden": "true" }, lines[key].slice(-4).map(function (l) { return h("div", {}, l); })) : null];
  }
  function btn(text, onClick, opts) {
    opts = opts || {};
    return h("button", { type: "button", class: opts.cls || "", disabled: !!opts.disabled, "data-k": opts.k || null,
                         on: { click: function (ev) { ev.preventDefault(); onClick(ev); } } },
      text);
  }
  function banner(text) { var b = $("banner"); b.hidden = !text; b.textContent = text || ""; }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function sentence(s) { s = String(s || "").trim(); return !s || /[.!?…)]$/.test(s) ? s : s + "."; }

  // ── server calls ───────────────────────────────────────────────────────
  function api(method, path, body) {
    return fetch(path, { method: method, cache: "no-store",
      headers: { "X-Copper-Token": TOKEN, "Content-Type": "application/json" },
      body: method === "POST" ? JSON.stringify(body || {}) : undefined })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) { var e = new Error(j.error || ("HTTP " + r.status)); e.fix = j.fix; throw e; } return j; }); });
  }
  function fail(key) { return function (e) { busy[key] = false; notes[key] = { cls: "bad", text: e.message, fix: e.fix }; render(); }; }
  function job(key, id, onDone, onTick) {
    busy[key] = true; lines[key] = []; render();
    (function poll() {
      api("GET", "/api/jobs/" + encodeURIComponent(id)).then(function (j) {
        if (onTick) onTick(j);
        lines[key] = j.lines || [];
        if (!j.done) { busy[key] = j.message || true; render(); return setTimeout(poll, 600); }
        busy[key] = false;
        // demo mode refuses downloads on purpose: say so plainly instead of as an error
        if (j.error) notes[key] = { cls: S && S.demo && key === "router" ? "" : "bad", text: j.error, fix: j.fix };
        else onDone(j.result || {});
        refresh();
      }).catch(fail(key));
    })();
  }
  function refresh() {
    return api("GET", "/api/state").then(function (s) { S = s; banner(""); render(); })
      .catch(function (e) { if (!closed) { banner("Copper setup stopped answering. Run “copper setup” again to reopen it."); if (window.console) console.warn("copper setup:", e); } });
  }

  // ── step status (for the rail) ─────────────────────────────────────────
  function kicadMajor() { var a = S.kicad && S.kicad.active; return a ? parseInt(String(a.version), 10) || 0 : 0; }
  function importable() { var p = S["import"]; return p ? (p.items || []).filter(function (i) { return i.state === "importable"; }) : []; }
  function connected() { return (S.apps || []).filter(function (a) { return a.state === "connected"; }); }
  function status(key) {
    if (!S) return "";
    switch (key) {
      case "kicad": return !S.kicad.found ? "warn" : kicadMajor() < 10 ? "warn" : "ok";
      case "import": return !S.kicad.found ? "" : (imported || (S.undo || []).length || !importable().length) ? "ok" : "";
      case "apps": return (S.apps || []).some(function (a) { return a.state === "broken"; }) ? "warn" : connected().length ? "ok" : "";
      case "github": return S.github && S.github.linked ? "ok" : "opt";
      case "router": return S.freerouting && S.freerouting.ok ? "ok" : "opt";
      case "test": return testResult ? "ok" : "";
    }
    return "";
  }
  var ST_TEXT = { ok: "done", warn: "needs a look", opt: "optional", "": "" };   // screen readers only

  function go(i, focus) {
    if (i < 0 || i >= STEPS.length) return;
    cur = i;
    render();
    if (focus !== false) { var hd = $(STEPS[i].id).querySelector("h2"); if (hd) hd.focus({ preventScroll: true }); }
    window.scrollTo(0, 0);
    $("announce").textContent = i <= LAST ? "Step " + (i + 1) + " of " + (LAST + 1) + ": " + STEPS[i].label : "Setup finished";
  }
  function next() { go(cur + 1); }
  function back() { go(cur - 1); }
  function navRow(key, primary, extra) {
    return [cur > 0 ? btn("Back", back, { cls: "quiet", k: key + "-back" }) : null, h("span", { class: "grow" }), extra || null, primary];
  }

  // ── 01 KiCad ───────────────────────────────────────────────────────────
  function renderKicad() {
    var k = S.kicad || {}, out = [];
    if (!k.found) {
      out.push(h("div", { class: "panel bad" }, h("h3", {}, "KiCad isn't installed yet"),
        h("p", {}, k.error ? k.error : "Copper needs KiCad 10 to design boards. It's free and takes a few minutes to install."),
        h("div", { class: "actions" },
          h("a", { class: "button", href: "https://www.kicad.org/download/", target: "_blank", rel: "noopener noreferrer" }, "Get KiCad 10"),
          btn("Check again", refresh, { k: "kicad-again" }))));
      out.push(noteEl(notes.kicad));
      fill("kicad-body", out);
      fill("kicad-nav", navRow("kicad", btn("Continue anyway", next, { k: "kicad-next", arrow: true })));
      return;
    }
    var a = k.active || {}, many = (k.installs || []).length > 1, ten = kicadMajor() >= 10;
    if (many) {
      out.push(h("p", { class: "muted" }, "More than one KiCad is installed. Pick the one Copper should use."));
      out.push(h("div", { class: "list", role: "radiogroup", "aria-label": "KiCad version" }, k.installs.map(function (i) {
        var on = a.root === i.root;
        return h("label", { class: "row" },
          h("input", { type: "radio", name: "kicad", checked: on, "data-k": "kicad-" + i.version, "aria-label": "Use KiCad " + i.version,
            on: { change: function () { api("POST", "/api/kicad/choose", { version: i.version }).then(function (s) { S = s; picks = {}; render(); }).catch(fail("kicad")); } } }),
          h("div", {}, h("div", { class: "title" }, "KiCad " + i.version), h("div", { class: "path" }, i.root)),
          h("div", { class: "right" }, on ? "In use" : null));
      })));
    } else {
      out.push(h("div", { class: "list", "aria-label": "KiCad found" },
        h("div", { class: "row" }, dot(ten ? "ok" : "warn", "!"),
          h("div", {}, h("div", { class: "title" }, "KiCad " + a.version), h("div", { class: "sub" }, ten ? "Ready to use." : "Can view and check designs only."),
            a.root ? h("div", { class: "path" }, a.root) : null), h("div", { class: "right" })),
        h("div", { class: "row" }, dot(a.kicad_cli ? "ok" : "warn", "!"),
          h("div", {}, h("div", { class: "title" }, "Command-line tools"),
            h("div", { class: "sub" }, a.kicad_cli ? "kicad-cli is there, so Copper can run KiCad's own checks and exports." : "kicad-cli is missing. Reinstalling KiCad usually brings it back.")),
          h("div", { class: "right" }))));
    }
    if (kicadMajor() && kicadMajor() < 10) {
      out.push(h("div", { class: "note warn" }, h("span", { class: "t" }, "KiCad " + a.version + " can view and check designs, but not edit them"),
        "Copper needs KiCad 10 to change schematics and boards. Install KiCad 10 next to it (KiCad " + a.version + " stays as it is), then press Check again. Your current settings can come along in the next step. ",
        h("a", { href: "https://www.kicad.org/download/", target: "_blank", rel: "noopener noreferrer" }, "kicad.org/download")));
    }
    if ((k.running || []).length) out.push(h("div", { class: "note warn" }, h("span", { class: "t" }, "KiCad is open"),
      "Close it before bringing settings across: KiCad saves its own settings when it exits and would undo the changes."));
    out.push(noteEl(notes.kicad));
    fill("kicad-body", out);
    fill("kicad-nav", navRow("kicad", btn("Continue", next, { cls: "primary", k: "kicad-next", arrow: true }),
      kicadMajor() < 10 ? btn("Check again", refresh, { k: "kicad-again" }) : null));
  }

  // ── 02 import ──────────────────────────────────────────────────────────
  function renderImport() {
    var p = S["import"], out = [];
    if (!p) {
      fill("import-body", [h("p", { class: "muted" }, "Nothing to bring along until KiCad is installed.")]);
      fill("import-nav", navRow("import", btn("Continue", next, { cls: "primary", k: "import-next", arrow: true })));
      return;
    }
    var items = p.items || [], can = items.filter(function (i) { return i.state === "importable"; });
    can.forEach(function (it) { if (!(it.id in picks)) picks[it.id] = !!(p.defaults || {})[it.id]; });
    if (p.same_version || !p.source || p.source === p.target) {
      $("import-lede").textContent = "Copper found your KiCad " + p.target + " settings and uses them as they are. A few extras can still be set up below.";
    } else {
      $("import-lede").textContent = "Copper found your KiCad " + p.source + " settings. Tick what you want in KiCad " + p.target +
        ". Every file is backed up first and one click puts it all back. Your KiCad " + p.source + " files are only read.";
    }
    if ((p.sources || []).length > 1) {
      var sel = h("select", { "aria-label": "Settings to bring along", "data-k": "import-source",
        on: { change: function () { picks = {}; api("POST", "/api/import/plan", { source: sel.value }).then(function (s) { S = s; render(); }).catch(fail("import")); } } },
        p.sources.map(function (v) { return h("option", { value: v, selected: v === p.source }, "KiCad " + v + (v === p.target ? " (current)" : "")); }));
      out.push(h("div", { class: "actions" }, h("span", { class: "muted" }, "Settings from"), sel));
    }
    var rows = items.filter(function (it) { return it.state !== "nothing"; }).map(function (it) {
      var c = it.state === "importable";
      var lead = c ? h("input", { type: "checkbox", checked: !!picks[it.id], "data-k": "imp-" + it.id, "aria-label": it.label + ": " + it.action,
                                   on: { change: function (ev) { picks[it.id] = ev.target.checked; render(); } } })
                   : it.state === "in-use" ? dot("ok") : dot("warn", "!");
      var body = h("div", {}, h("div", { class: "title" }, it.label),
        h("div", { class: "sub" }, (it.id === "design_defaults" ? it.found : cap(it.found)) + (it.state === "in-use" ? ". Already in use." : "")),
        it.state === "blocked" ? h("div", { class: "act" }, cap(it.blocked))
          : (c && it.action ? h("div", { class: "act" }, cap(it.action),
              h("span", { class: "kind" }, it.modifies_kicad ? " · changes KiCad's settings" : " · kept in Copper's folder")) : null));
      return h(c ? "label" : "div", { class: "row" }, lead, body, h("div", { class: "right" }));
    });
    if (rows.length) out.push(h("div", { class: "list" }, rows));
    else out.push(h("p", { class: "muted" }, "No KiCad settings were found to bring along. That's fine: Copper sets up what it needs as it goes."));
    (p.warnings || []).forEach(function (w) { out.push(h("div", { class: "note" }, rich(w))); });
    if (busy["import"]) out.push(working("Copying and backing up…", "import"));
    out.push(noteEl(notes["import"]));
    var undo = (S.undo || [])[0];
    var n = can.filter(function (it) { return picks[it.id]; }).length;
    var primary = imported || !can.length
      ? btn("Continue", next, { cls: "primary", k: "import-next", arrow: true })
      : btn(n ? "Bring " + plural(n, "item", "items") + " along" : "Continue without importing", n ? applyImport : function () { imported = true; next(); },
            { cls: "primary", k: "import-apply", disabled: !!busy["import"], arrow: !n });
    fill("import-body", out);
    fill("import-nav", navRow("import", primary,
      [undo ? btn("Undo the import", undoImport, { k: "import-undo", disabled: !!busy["import"] }) : null,
       !imported && can.length && n ? btn("Skip", function () { imported = true; next(); }, { cls: "quiet", k: "import-skip" }) : null]));
  }
  function applyImport() {
    busy["import"] = true; notes["import"] = null; render();
    api("POST", "/api/import/apply", { choices: picks }).then(function (r) {
      var labels = {};
      ((S["import"] || {}).items || []).forEach(function (it) { labels[it.id] = it.label; });
      busy["import"] = false; S = r.state; picks = {};
      var res = r.result.results || {}, errs = [], extra = [];
      Object.keys(res).forEach(function (k) {
        var l = labels[k] || k;
        if (res[k].error) errs.push(l + ": " + sentence(res[k].error));
        if (res[k].note) extra.push(l + ": " + sentence(res[k].note));
        if (res[k].skipped) extra.push(l + ": " + sentence(res[k].skipped));
      });
      var n = (r.result.changed || []).length;
      if (errs.length) notes["import"] = { cls: "bad", title: "Some items could not be brought along", text: errs.join(" ") };
      else { imported = true; notes["import"] = { cls: "ok", title: n ? plural(n, "file", "files") + " updated. The originals are backed up." : "Nothing needed changing.", text: extra.join(" ") }; }
      render();
    }).catch(fail("import"));
  }
  function undoImport() {
    busy["import"] = true; render();
    api("POST", "/api/import/undo", {}).then(function (r) {
      busy["import"] = false; S = r.state; picks = {}; imported = false;
      var u = r.result;
      notes["import"] = u.refused ? { cls: "warn", text: u.refused }
        : { cls: (u.kept || []).length ? "warn" : "ok", title: "Put back as it was.", text: u.detail + ((u.notes || []).length ? ". " + u.notes.join("; ") : "") +
            ((u.kept || []).length ? ". Left alone: " + u.kept.map(function (k) { return k.path + " (" + k.why + ")"; }).join("; ") : "") };
      render();
    }).catch(fail("import"));
  }

  // ── 03 AI apps ─────────────────────────────────────────────────────────
  function renderApps() {
    var out = [], apps = S.apps || [];
    if (!appPicks) { appPicks = {}; apps.forEach(function (a) { appPicks[a.id] = a.state === "found" || a.state === "broken"; }); }
    var shown = apps.filter(function (a) { return a.state !== "not-found" || appPicks[a.id]; });
    var missing = apps.filter(function (a) { return a.state === "not-found"; });
    var running = !!busy.apps;
    if (!apps.some(function (a) { return a.state !== "not-found"; })) {
      out.push(h("div", { class: "panel warn" }, h("h3", {}, "No AI app found on this computer"),
        h("p", {}, "Copper works with Claude Desktop, Claude Code, OpenAI Codex, Cursor, VS Code, Windsurf, Zed, Gemini CLI and GitHub Copilot CLI. Install one, then press Check again. Or tick one below to prepare its settings now."),
        h("div", { class: "actions" }, btn("Check again", function () { appPicks = null; refresh(); }, { k: "apps-again" }))));
    }
    if (shown.length) out.push(h("div", { class: "list" }, shown.map(function (a) {
      var con = a.state === "connected", t = tested[a.id];
      var going = running && appPicks[a.id] && !con;
      var lead = con ? dot("ok") : going ? dot("busy", h("span", { class: "spin" }))
        : h("input", { type: "checkbox", checked: !!appPicks[a.id], "data-k": "app-" + a.id, "aria-label": "Connect " + a.name, disabled: running,
                       on: { change: function (ev) { appPicks[a.id] = ev.target.checked; render(); } } });
      var sub = con ? (t ? "Connected and tested. It " + t + "." : a.via_plugin ? a.detail : "Connected.")
        : going ? "Connecting…"
        : a.state === "broken" ? "Needs repair: " + a.detail
        : a.state === "not-found" ? "Not installed yet. Its settings file will be ready for when you install it."
        : "Found on this computer.";
      return h(con || running ? "div" : "label", { class: "row" }, lead,
        h("div", {}, h("div", { class: "title" }, a.name), h("div", { class: "sub" + (a.state === "broken" && !going ? " bad" : "") }, rich(sub)),
          (a.paths || [])[0] ? h("div", { class: "path" }, a.paths[0]) : null),
        h("div", { class: "right" },
          con && !a.via_plugin && !running ? btn("Disconnect", function () { disconnect(a.id); }, { cls: "link", k: "dis-" + a.id }) : null));
    })));
    if (running) out.push(working(typeof busy.apps === "string" ? busy.apps : "Connecting, then starting Copper the way each app will…", "apps"));
    out.push(noteEl(notes.apps));
    if (missing.length) {
      var none = missing.length === apps.length;
      out.push(h("details", { open: none || !!document.querySelector("#apps-body details[data-d=miss][open]"), "data-d": "miss" },
        h("summary", {}, none ? "Prepare an app you are about to install" : plural(missing.length, "other supported app", "other supported apps") + " not found on this computer"),
        h("div", { class: "inner" }, h("p", { class: "muted" }, "Installing one later? Tick it to prepare its settings file now."),
          h("div", { class: "list" }, missing.map(function (a) { return h("label", { class: "row" },
            h("input", { type: "checkbox", checked: !!appPicks[a.id], "data-k": "miss-" + a.id, "aria-label": "Prepare " + a.name, on: { change: function (ev) { appPicks[a.id] = ev.target.checked; render(); } } }),
            h("div", {}, h("div", { class: "title" }, a.name), (a.paths || [])[0] ? h("div", { class: "path" }, a.paths[0]) : null), h("div", {})); })))));
    }
    out.push(h("details", { open: !!document.querySelector("#apps-body details[data-d=other][open]"), "data-d": "other" },
      h("summary", {}, "Using another app that supports MCP?"),
      h("div", { class: "inner" }, h("p", { class: "muted" }, "Paste this into its MCP server settings:"), h("pre", {}, S.snippet || ""),
        h("div", { class: "actions" }, btn("Copy", function () { copy(S.snippet || "", "apps"); }, { k: "apps-copy" })))));
    fill("apps-body", out);
    var want = apps.filter(function (a) { return a.state !== "connected" && appPicks[a.id]; }).length;
    var primary = want ? btn("Connect " + (want > 1 ? want + " apps" : "1 app"), connect, { cls: "primary", k: "apps-connect", disabled: running })
                       : btn("Continue", next, { cls: "primary", k: "apps-next", arrow: true, disabled: running });
    fill("apps-nav", navRow("apps", primary, [
      connected().length ? btn("Test again", verifyApps, { k: "apps-verify", disabled: running }) : null,
      want && connected().length ? btn("Continue", next, { cls: "quiet", k: "apps-skip", disabled: running }) : null,
      !want && !connected().length ? null : null]));
  }
  function connect() {
    var ids = (S.apps || []).filter(function (a) { return a.state !== "connected" && appPicks[a.id]; }).map(function (a) { return a.id; });
    if (!ids.length) { notes.apps = { cls: "", text: "Tick at least one app first." }; return render(); }
    notes.apps = null;
    api("POST", "/api/apps/connect", { ids: ids }).then(function (r) {
      job("apps", r.job, function (res) {
        var bad = (res.verify || []).filter(function (v) { return !v.ok; }), errs = (res.changes || []).filter(function (c) { return c.action === "error"; });
        appPicks = null;
        (res.verify || []).forEach(function (v) { if (v.ok) tested[v.client] = v.detail; else delete tested[v.client]; });
        if (errs.length) notes.apps = { cls: "bad", title: "Some settings files could not be written", text: errs.map(function (c) { return c.client + ": " + (c.detail || c.path); }).join(" · "), fix: "copper doctor" };
        else if (bad.length) notes.apps = { cls: "bad", title: "Registered, but Copper does not start for " + bad.map(function (v) { return v.name; }).join(", "),
                                            text: bad.map(function (v) { return v.stage + ": " + v.detail; }).join(" · "), fix: "copper doctor" };
        else {
          var names = (res.verify || []).map(function (v) { return v.name; }).filter(function (v, i, a) { return a.indexOf(v) === i; });
          notes.apps = { cls: "ok", title: "Connected and tested: " + names.join(", ") + ".", text: "Each app picks Copper up the next time it starts." };
        }
      });
    }).catch(fail("apps"));
  }
  function disconnect(id) {
    api("POST", "/api/apps/disconnect", { ids: [id] }).then(function (r) { S = r.state; appPicks = null; delete tested[id];
      notes.apps = { cls: "", text: "Disconnected. The backup of the original settings file (*.copper-backup) is kept." }; render(); }).catch(fail("apps"));
  }
  function verifyApps() {
    notes.apps = null;
    api("POST", "/api/apps/verify", {}).then(function (r) {
      job("apps", r.job, function (res) {
        var rows = (res.apps || []).filter(function (a) { return a.checked || a.state === "broken"; });
        tested = {};
        rows.forEach(function (a) { if (a.checked && a.state === "connected") tested[a.id] = a.detail; });
        var bad = rows.filter(function (a) { return a.state === "broken"; });
        notes.apps = !rows.length ? { cls: "", text: "No app is connected yet." }
          : bad.length ? { cls: "bad", title: "Not working yet", text: bad.map(function (a) { return a.name + ": " + a.detail; }).join(" · "), fix: "copper doctor" }
          : { cls: "ok", title: "All connected apps start Copper correctly." };
      });
    }).catch(fail("apps"));
  }

  // ── 04 GitHub ──────────────────────────────────────────────────────────
  function renderGithub() {
    var g = S.github || {}, out = [];
    out.push(h("div", { class: "list" }, h("div", { class: "row" }, dot(g.linked ? "ok" : "", g.linked ? "✓" : "–"),
      h("div", {}, h("div", { class: "title" }, g.linked ? "Linked" + (g.user ? " as " + g.user : "") : "Not linked"),
        h("div", { class: "sub" }, g.linked ? rich(g.detail) : g.how === "gh" ? "Your GitHub CLI is signed in. Copper can use that login."
          : g.how === "none" ? ["Install the ", h("a", { href: "https://cli.github.com/", target: "_blank", rel: "noopener noreferrer" }, "GitHub CLI"),
                                 ", run ", h("code", {}, "gh auth login"), " in a terminal, then press Check again."]
          : rich(g.explain || ""))),
      h("div", { class: "right" }, g.linked ? btn("Unlink", function () {
        api("POST", "/api/github/unlink", {}).then(function (r) { S = r.state; notes.github = { cls: "", text: r.result.note || "Unlinked." }; render(); }).catch(fail("github")); }, { cls: "link", k: "gh-unlink" }) : null))));
    if (ghCode && busy.github) {
      out.push(h("div", { class: "panel" }, h("h3", {}, "Type this code on GitHub"), h("div", { class: "code-big", "aria-label": "Sign-in code" }, ghCode.user_code),
        h("p", {}, h("a", { href: ghCode.verification_uri, target: "_blank", rel: "noopener noreferrer" }, ghCode.verification_uri), " · Copper never sees your GitHub password."),
        h("div", { class: "actions" }, btn("Cancel", function () { api("POST", "/api/github/cancel", {}); }, { k: "gh-cancel" }))));
    } else if (busy.github) out.push(working("Contacting GitHub…", "github"));
    if (!g.linked && g.how === "none") out.push(h("details", { open: !!document.querySelector("#github-body details[open]") },
      h("summary", {}, "Signing in without the GitHub CLI"),
      h("div", { class: "inner" }, h("p", { class: "muted" }, "That needs a GitHub OAuth app id, which this build of Copper doesn't include. If your organisation has one, set ",
        h("code", {}, "COPPER_GITHUB_CLIENT_ID"), " and run ", h("code", {}, "copper setup"), " again."))));
    if (!g.linked && g.how === "gh-login") out.push(h("div", { class: "note" }, "Run ", h("code", {}, "gh auth login"), " in a terminal (it signs you in on github.com), then press Check again."));
    out.push(noteEl(notes.github));
    fill("github-body", out);
    var canLink = !g.linked && (g.how === "gh" || g.how === "device");
    fill("github-nav", navRow("github",
      canLink ? btn(g.how === "gh" ? "Link with my GitHub CLI login" : "Link GitHub", linkGithub, { cls: "primary", k: "gh-link", disabled: !!busy.github })
              : btn(g.linked ? "Continue" : "Skip for now", next, { cls: "primary", k: "gh-next", arrow: true }),
      [!g.linked ? btn("Check again", refresh, { k: "gh-again" }) : null, canLink ? btn("Skip", next, { cls: "quiet", k: "gh-skip" }) : null]));
  }
  function linkGithub() {
    ghCode = null; notes.github = null;
    api("POST", "/api/github/link", {}).then(function (r) {
      job("github", r.job, function (res) { ghCode = null; notes.github = { cls: "ok", text: res.detail || "Linked." }; },
        function (j) { if (j.user_code && !ghCode) ghCode = { user_code: j.user_code, verification_uri: j.verification_uri }; if (j.done) ghCode = null; });
    }).catch(fail("github"));
  }

  // ── 05 autorouter ──────────────────────────────────────────────────────
  function renderRouter() {
    var f = S.freerouting || {}, out = [];
    out.push(h("div", { class: "list" }, h("div", { class: "row" }, dot(f.ok ? "ok" : "", f.ok ? "" : "–"),
      h("div", {}, h("div", { class: "title" }, f.ok ? "Freerouting is installed" : "Freerouting"), h("div", { class: "sub" }, cap(sentence(f.summary || f.detail || "")))),
      h("div", { class: "right" }))));
    if (busy.router) out.push(working(typeof busy.router === "string" ? busy.router : "Downloading…", "router"));
    out.push(noteEl(notes.router));
    if (!f.ok && !busy.router && !notes.router) out.push(h("p", { class: "muted legend" }, "Skipping is fine. Nothing else in Copper needs it."));
    fill("router-body", out);
    var later = btn(f.ok ? "Continue" : "Later", next, { cls: f.ok ? "primary" : "", k: "router-next", disabled: !!busy.router });
    var dl = f.ok ? null : btn("Download now", function () {
        notes.router = null;
        api("POST", "/api/freerouting", {}).then(function (r) { job("router", r.job, function (res) { notes.router = { cls: res.ok ? "ok" : "warn", text: res.detail }; }); }).catch(fail("router"));
      }, { cls: "primary", k: "router-dl", disabled: !!busy.router });
    fill("router-nav", navRow("router", dl || later, dl ? later : null));
  }

  // ── 06 test ────────────────────────────────────────────────────────────
  function renderTest() {
    var out = [];
    if (busy.test) out.push(working(typeof busy.test === "string" ? busy.test : "Starting Copper…", "test"));
    if (testResult) {
      out.push(h("div", { class: "term", role: "group", "aria-label": "Copper's answer" },
        h("div", {}, h("span", { class: "p" }, "$ "), "copper_status"),
        h("div", { class: "ok" }, testResult.detail),
        testResult.reply ? h("div", { class: "dim" }, testResult.reply) : null));
    }
    out.push(noteEl(notes.test));
    if (!testResult && !busy.test) out.push(h("p", { class: "muted legend" }, "It takes a few seconds and changes nothing."));
    fill("test-body", out);
    fill("test-nav", navRow("test", testResult ? btn("Finish", next, { cls: "primary", k: "test-next", arrow: true })
      : btn("Run the test call", runTest, { cls: "primary", k: "test-run", disabled: !!busy.test }),
      testResult ? btn("Run again", runTest, { k: "test-again", disabled: !!busy.test }) : btn("Skip", next, { cls: "quiet", k: "test-skip" })));
  }
  function runTest() {
    notes.test = null;
    api("POST", "/api/test", {}).then(function (r) {
      job("test", r.job, function (res) {
        var reply = (res.reply || "").split(/\r?\n/).filter(function (l) { l = l.trim(); return l && l[0] !== "{" && l[0] !== "["; }).slice(0, 2).join("  ");
        testResult = { detail: "Copper " + res.detail, reply: reply.length > 220 ? reply.slice(0, 217) + "…" : reply };
      });
    }).catch(fail("test"));
  }

  // ── ready ──────────────────────────────────────────────────────────────
  function renderDone() {
    var out = [], con = connected();
    $("done-lede").textContent = con.length ? "Open " + (con.length === 1 ? con[0].name : "your AI app") + " and ask it for a board. Here's a good first one:"
      : "Connect an AI app first (the AI apps step), then ask it for a board. Here's a good first one:";
    var copied = notes.done && notes.done.cls === "ok";
    out.push(h("div", { class: "prompt" }, h("p", { class: "text" }, S.example || ""),
      h("div", { class: "actions" }, btn(copied ? "Copied" : "Copy prompt", function () { copy(S.example || "", "done"); }, { k: "done-copy" }),
        notes.done && !copied ? noteEl(notes.done) : null)));
    var items = [];
    if (S.kicad && S.kicad.found && S.kicad.active) items.push(["ok", "KiCad " + S.kicad.active.version, kicadMajor() >= 10 ? "Ready." : "View and check only. Install KiCad 10 to edit."]);
    con.forEach(function (a) { items.push(["ok", a.name, a.restart || "Restart it to load Copper."]); });
    if (!con.length) items.push(["warn", "No AI app connected", "Run `copper link` when you have one."]);
    if (testResult) items.push(["ok", "Test call", cap(testResult.detail.replace(/^Copper /, "")) + "."]);
    items.push([S.freerouting && S.freerouting.ok ? "ok" : "", "Autorouter", S.freerouting && S.freerouting.ok ? "Installed." : "Downloads the first time it's needed."]);
    out.push(h("ul", { class: "checks", "aria-label": "Summary" }, items.map(function (it) {
      return h("li", {}, dot(it[0], it[0] === "warn" ? "!" : "–"), h("span", {}, h("b", {}, it[1]), " ", h("span", { class: "d" }, rich(it[2]))));
    })));
    fill("done-body", out);
    fill("done-nav", navRow("done", btn("Close setup", finish, { cls: "primary", k: "done-close" })));
  }
  function finish() {
    closed = true;
    api("POST", "/api/done", {}).then(function () {
      var m = $("main"); m.textContent = "";
      m.appendChild(h("section", { class: "step closed" },
        h("h2", { tabindex: "-1" }, "You can close this tab"),
        h("p", { class: "lede" }, "Copper keeps running inside your AI apps. Open this page again any time with ", h("code", {}, "copper setup"), ".")));
      $("bar").style.setProperty("width", "100%"); $("where").textContent = "";
      document.querySelector(".steps").style.visibility = "hidden";   // nothing left to go back to
      var hd = m.querySelector("h2"); if (hd) hd.focus({ preventScroll: true });
    }).catch(function () { window.close(); });
  }

  function copy(text, key) {
    var ok = function () { notes[key] = { cls: "ok", text: "Copied." }; render(); setTimeout(function () { notes[key] = null; render(); }, 2600); };
    var no = function () { notes[key] = { cls: "warn", text: "Copying was blocked by the browser; select the text and copy it." }; render(); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(ok, no); else no();
  }

  // ── rail + frame ───────────────────────────────────────────────────────
  function renderRail() {
    fill("stepnav", STEPS.slice(0, LAST + 1).map(function (s, i) {
      var st = status(s.key);
      return h("li", { class: st }, h("button", { type: "button", "aria-current": i === cur ? "step" : null, "data-k": "nav-" + s.key,
        "aria-label": "Step " + (i + 1) + ": " + s.label + (ST_TEXT[st] ? " (" + ST_TEXT[st] + ")" : ""), on: { click: function () { go(i); } } },
        h("span", { class: "ic", "aria-hidden": "true" }, st === "ok" ? tick() : st === "warn" ? "!" : null), h("span", { class: "label" }, s.label)));
    }));
    $("where").textContent = cur > LAST ? "" : "Step " + (cur + 1) + " of " + (LAST + 1);
    $("bar").style.setProperty("width", (cur > LAST ? 100 : Math.round((cur + 1) / (LAST + 2) * 100)) + "%");
    STEPS.forEach(function (s, i) { $(s.id).hidden = i !== cur; });
    if (S && S.demo) $("foot-text").textContent = "Demo mode. Everything happens in a throw-away folder, and this computer's real settings are not touched.";
  }

  var RENDER = { kicad: renderKicad, "import": renderImport, apps: renderApps, github: renderGithub, router: renderRouter, test: renderTest, done: renderDone };
  function render() {
    if (!S || closed) return;
    var a = document.activeElement, k = a && a.getAttribute && a.getAttribute("data-k");
    renderRail();
    RENDER[STEPS[cur].key]();
    if (k) { var el = document.querySelector('[data-k="' + k.replace(/"/g, '\\"') + '"]'); if (el && el !== a && !el.disabled) el.focus({ preventScroll: true }); }
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.altKey && ev.key === "ArrowRight") { ev.preventDefault(); next(); }
    else if (ev.altKey && ev.key === "ArrowLeft") { ev.preventDefault(); back(); }
  });

  // ── lifetime ───────────────────────────────────────────────────────────
  setInterval(function () { if (!hidden && !closed) api("POST", "/api/heartbeat", {}).catch(function () {}); }, 20000);
  document.addEventListener("visibilitychange", function () { hidden = document.hidden; if (!hidden && !closed) refresh(); });
  window.addEventListener("pagehide", function () {
    try { navigator.sendBeacon("/api/bye?t=" + encodeURIComponent(TOKEN), new Blob(["{}"], { type: "application/json" })); } catch (e) { /* closing anyway */ }
  });
  // A reload sends "bye" from the old page; that beacon can arrive after this page's first requests.
  // Two early heartbeats make sure the server hears from the new page inside its 5 s grace period.
  [1000, 3000].forEach(function (ms) { setTimeout(function () { api("POST", "/api/heartbeat", {}).catch(function () {}); }, ms); });
  STEPS.forEach(function (s, i) { $(s.id).hidden = i !== 0; });
  refresh();
})();
