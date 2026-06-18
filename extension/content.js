// AutonomousHire autofill — runs on demand (injected by the popup) in the page the
// user already has open. Scrapes form fields, asks the local backend for answers, fills
// them, and shows an overlay of what was filled vs. what needs the user. No submission —
// the user reviews, solves any CAPTCHA, and submits natively.
(async () => {
  // Idempotency: a frame can be injected twice (Fill-All direct-inject + the discard re-arm, or a
  // re-click). Skip if a fill is already running here, or finished < 20s ago — so we never
  // double-scrape or double-attach the résumé.
  if (window.__ahRunning) return;
  if (window.__ahDoneAt && Date.now() - window.__ahDoneAt < 20000) return;
  window.__ahRunning = true;
  const _ahDone = () => { window.__ahRunning = false; window.__ahDoneAt = Date.now(); };
  // Instant proof-of-injection badge: appears the MOMENT this frame is injected (DOM ops aren't
  // throttled in background tabs), so when you switch to a Fill-All'd tab you can SEE it was hit —
  // even before the throttled fill finishes. showOverlay() at the end replaces it.
  try {
    if (document.body && !document.getElementById("__ah_badge")) {
      const _b = document.createElement("div");
      _b.id = "__ah_badge";
      _b.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;background:#1e3a8a;" +
        "color:#dbeafe;font:12px -apple-system,sans-serif;padding:8px 12px;border-radius:8px;" +
        "box-shadow:0 8px 30px rgba(0,0,0,.5)";
      _b.textContent = "AutonomousHire: filling this form…";
      document.body.appendChild(_b);
    }
  } catch (_) {}
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/\*+/g, "").trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
  };
  // Radios/checkboxes are frequently the NATIVE input hidden behind a styled circle
  // (Ashby, Greenhouse, many design systems). The input has ~0 size so `visible()` is
  // false — check its label/wrapper instead, otherwise we miss the whole question.
  const ctrlPresent = (el) => {
    if (visible(el)) return true;
    const lab = el.closest("label")
      || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`))
      || el.parentElement;
    return !!(lab && visible(lab) && !el.disabled);
  };

  // ── Label detection ────────────────────────────────────────────────────────
  function labelFor(el) {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && clean(l.textContent).length > 1) return clean(l.textContent);
    }
    const lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(" ").map((id) => document.getElementById(id)).filter(Boolean)
        .map((n) => n.textContent).join(" ");
      if (clean(t).length > 1) return clean(t);
    }
    const al = el.getAttribute && el.getAttribute("aria-label");
    if (al && clean(al).length > 1) return clean(al);
    const wrap = el.closest && el.closest("label");
    if (wrap && clean(wrap.textContent).length > 1) return clean(wrap.textContent);
    let p = el;
    for (let i = 0; i < 6 && p; i++) {
      p = p.parentElement;
      if (!p) break;
      const l = p.querySelector("label, legend, [class*='label'], [class*='question']");
      if (l && !l.querySelector("input,select,textarea") && clean(l.textContent).length > 2)
        return clean(l.textContent);
    }
    return clean((el.getAttribute && el.getAttribute("placeholder")) || "");
  }

  // ── Group (radio/checkbox) helpers ───────────────────────────────────────────
  // The text of ONE option (a single radio/checkbox's own label).
  function optionText(el) {
    let l = "";
    if (el.id) { const le = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (le) l = clean(le.textContent); }
    if (!l) { const w = el.closest("label"); if (w) l = clean(w.textContent); }
    if (!l) l = clean((el.getAttribute && el.getAttribute("aria-label")) || "");
    // Radio/checkbox with NO <label> — Kforce renders "<input> I require sponsorship…" as a sibling
    // text/element. Read the adjacent text, then fall back to the value attr ("NeedsSponsorship"),
    // which encodes the meaning. Without this the group's options are blank → it gets skipped.
    if (!l && el.nextSibling) l = clean(el.nextSibling.textContent || el.nextSibling.nodeValue || "");
    if (!l && el.parentElement) { const pt = clean(el.parentElement.textContent || ""); if (pt && pt.length <= 140) l = pt; }
    if (!l) l = clean((el.getAttribute && el.getAttribute("value")) || "");
    return l;
  }
  function isOptionText(t, opts) {
    const tl = (t || "").toLowerCase();
    return opts.some((o) => optionText(o).toLowerCase() === tl);
  }
  // The QUESTION label for a group of option inputs — NOT the first option's text.
  // Ashby/Greenhouse render the question as a heading/label ABOVE the options (often a
  // sibling of the options container, not a <fieldset><legend>), so labelFor(firstOption)
  // wrongly returns the option text. Walk up to the field wrapper and find the nearest
  // text element that wraps NO option input.
  function questionLabel(opts) {
    if (!opts.length) return "";
    const fs = opts[0].closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); const t = lg && clean(lg.textContent);
      if (t && !isOptionText(t, opts)) return t; }
    const grp = opts[0].closest("[role='radiogroup'],[role='group']");
    if (grp) { const t = labelFor(grp); if (t && t.length < 280 && !isOptionText(t, opts)) return t; }
    // preferred selectors first (label/legend/heading), then generic text nodes
    const PREF = "label,legend,h1,h2,h3,h4,h5,h6,[class*='label'],[class*='Label'],[class*='title'],[class*='Title'],[class*='question'],[class*='Question']";
    const GEN = "p,strong,b,div,span";
    let el = opts[0].parentElement;
    for (let i = 0; i < 9 && el; i++) {
      if (opts.every((o) => el.contains(o))) {
        for (const sel of [PREF, GEN]) {
          const cand = [...el.querySelectorAll(sel)].find((n) =>
            !n.querySelector("input,textarea,select,[role='radio'],[role='checkbox']")
            && !opts.some((o) => n.contains(o))
            && clean(n.textContent).length > 2 && clean(n.textContent).length < 280
            && !isOptionText(clean(n.textContent), opts));
          if (cand) return clean(cand.textContent);
        }
      }
      el = el.parentElement;
    }
    return "";
  }

  // ── React-Select helpers ─────────────────────────────────────────────────────
  const isReactSelect = (el) =>
    (el.className || "").toString().includes("select__control") ||
    /\b[\w-]*__control\b/.test((el.className || "").toString());

  function rsOpen(control) {
    // React-Select opens on mousedown of the control (or its inner input), not a plain click.
    const t = control.querySelector("input") || control;
    for (const type of ["mousedown", "mouseup", "click"])
      t.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
  }
  function rsClose(control) {
    try { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (e) {}
    try { control.querySelector("input")?.blur(); } catch (e) {}
  }
  // Dismiss EVERY open react-select menu — call before opening one so portaled menus can't
  // stack and cross-contaminate (the "No" option leaking into the Gender dropdown).
  function closeAllMenus() {
    try { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (e) {}
    try { const a = document.activeElement; if (a && a.blur) a.blur(); } catch (e) {}
  }

  const committed = (control) =>
    !!control.querySelector(".select__single-value, [class*='__single-value'], " +
                            ".select__multi-value, [class*='__multi-value']");

  // The menu belonging to THIS control — via the input's aria-controls (covers portaled
  // menus) or the menu rendered inside the control's own select wrapper. Scoping this is
  // critical: a page-wide option query also grabs OTHER dropdowns (e.g. a phone
  // country-code list), which is how Gender got "Cayman Islands +1".
  function menuFor(control) {
    const inp = control.querySelector("input");
    const lid = inp && (inp.getAttribute("aria-controls") || inp.getAttribute("aria-owns"));
    if (lid) { const m = document.getElementById(lid); if (m) return m; }
    let w = control;
    for (let i = 0; i < 4 && w; i++) {
      w = w.parentElement; if (!w) break;
      const m = w.querySelector(".select__menu, [class*='__menu']");
      if (m) return m;
    }
    // Last resort: Greenhouse portals the open menu to <body> with no aria link back to the
    // control. Since we close every other menu before opening this one, exactly one menu is
    // open in the document — that's ours. (If somehow >1 is open, return null rather than risk
    // grabbing a neighbour's options — the "No" bleeding into the Gender menu bug.)
    const open = [...document.querySelectorAll(".select__menu, [class*='__menu']")].filter(visible);
    return open.length === 1 ? open[0] : null;
  }
  function optionEls(control) {
    const menu = menuFor(control);
    if (!menu) return [];
    return [...menu.querySelectorAll("[role='option'], .select__option, [class*='__option']")];
  }

  // Best label for a react-select. Greenhouse's Remix EEO selects (race/gender/…) don't expose the
  // question via labelFor — the "Please identify your race" heading sits in a separate field
  // wrapper. Fall back to: question-wrapper walk → aria-labelledby target → aria-label → the
  // react-select-{key}-input id (race→"Race") → a hidden input[name]. Without this the field has no
  // label and gets SKIPPED entirely (the "race not answered" bug).
  function reactSelectLabel(ctrl, inp) {
    let l = labelFor(ctrl) || questionLabel([ctrl]);
    if (l) return l;
    const lb = inp && inp.getAttribute("aria-labelledby");
    if (lb) {
      const txt = lb.split(/\s+/).map((id) => { const e = document.getElementById(id); return e ? clean(e.textContent) : ""; })
        .filter(Boolean).join(" ").replace(/\s*(Race & Ethnicity Definitions|select\.\.\.).*/i, "").trim();
      if (txt) return txt;
    }
    l = (inp && clean(inp.getAttribute("aria-label") || "")) || clean(ctrl.getAttribute("aria-label") || "");
    if (l) return l;
    // react-select-{key}-input  →  "race" / "hispanic_ethnicity" / "veteran_status"
    const idstr = (inp && inp.id) || (ctrl.querySelector("[id*='react-select-']") || {}).id || "";
    const m = idstr.match(/react-select-([a-z0-9_]+)-(?:input|live-region|listbox|placeholder)/i);
    if (m) return m[1].replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());  // race → "Race"
    const named = ctrl.querySelector("input[name]") || (ctrl.closest("[class*='field'],[class*='question'],div") || document).querySelector("input[name]");
    if (named && named.name && !/^react-select/.test(named.name)) return named.name.replace(/[_-]+/g, " ");
    return "";
  }

  async function readReactOptions(control) {
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        closeAllMenus();
        await sleep(60);
        rsOpen(control);
        await sleep(attempt === 0 ? 420 : 750);
        const opts = optionEls(control).map((o) => clean(o.textContent)).filter(Boolean);
        rsClose(control);
        await sleep(120);
        if (opts.length) return [...new Set(opts)];
      }
    } catch (e) {}
    return [];
  }

  function setInputValue(inp, val) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(inp, val);
    inp.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function fillReactSelect(control, value) {
    const v = clean(value);
    const inp = control.querySelector("input");
    const pick = () => {
      const all = optionEls(control);
      // exact → option-contains-value. No reverse (value-contains-option) match — it would
      // pick "Hispanic or Latino" for the value "Asian (Not Hispanic or Latino)".
      const hit = all.find((o) => clean(o.textContent) === v)
        || all.find((o) => { const t = clean(o.textContent); return t.length > 3 && t.includes(v); })
        || (v.length > 6 && all.find((o) => { const t = clean(o.textContent);
             return t.length > 6 && v.startsWith(t); }));
      if (hit) return hit;
      // Numeric → range: a bare "6" should select the bucket "6-9 years" rather than typing
      // "6" into the input (which filters but never commits, leaving the menu hung open).
      if (/^\d+$/.test(v)) {
        const n = parseInt(v, 10);
        return all.find((o) => {
          const t = clean(o.textContent).toLowerCase();
          const nums = (t.match(/\d+/g) || []).map(Number);
          if (!nums.length) return false;
          if (nums.length >= 2) return n >= nums[0] && n <= nums[1];
          if (/\+|more than|at least|over|or more/.test(t)) return n >= nums[0];
          if (/less than|under|fewer|up to|below|or less/.test(t)) return n <= nums[0] - 1;
          return n === nums[0];
        });
      }
      return null;
    };
    const clickOpt = (opt) => {
      opt.scrollIntoView({ block: "nearest" });
      // React-Select selects on mousedown; dispatch the full sequence to be safe.
      for (const t of ["pointerdown", "mousedown", "mouseup", "click"])
        opt.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    };
    try {
      closeAllMenus();          // dismiss any neighbour's open menu first (no cross-contamination)
      await sleep(80);
      rsOpen(control);
      // Poll for options to render — async/lazy menus (and EEO dropdowns far down a long page)
      // don't paint within a fixed 400ms, which is why race was intermittently left empty.
      let opt = null;
      for (let i = 0; i < 10 && !opt; i++) { await sleep(150); opt = pick(); }
      if (opt) { clickOpt(opt); await sleep(180); }
      // Keyboard fallback: type to filter the list, then CLICK the matched option (blind Enter
      // is unreliable when the focused row isn't the one we want). Poll after typing for async lists.
      if (!committed(control) && inp) {
        rsOpen(control);
        await sleep(150);
        inp.focus();
        setInputValue(inp, v.slice(0, 18));
        let o2 = null;
        for (let i = 0; i < 12 && !o2; i++) { await sleep(120); o2 = pick(); }
        if (o2) { clickOpt(o2); await sleep(160); }
        else {
          inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, which: 13, bubbles: true }));
          await sleep(180);
        }
        // If still not committed, clear the typed filter text so we don't leave a half-typed
        // value ("6") sitting in an open, filtered menu — the visible "stuck open" symptom.
        if (!committed(control)) { try { setInputValue(inp, ""); } catch (e) {} }
      }
      rsClose(control);
      await sleep(60);
      if (!committed(control)) rsClose(control);   // belt-and-suspenders: never leave it hanging
      return committed(control);
    } catch (e) {}
    return false;
  }

  // ── Native value setter (React-friendly) ─────────────────────────────────────
  // Setting .value alone leaves React's internal state stale, so the form still flags the field
  // "required / missing" even though it looks filled (the Ashby "Missing entry for Name" case).
  // Fix: (1) reset React's _valueTracker so it detects the change, (2) set via the native setter,
  // (3) fire input+change, (4) focus/blur so the field is marked "touched" and validation re-runs.
  // A phone field often has a JS input MASK (Kforce). Bulk-setting "555-010-0100" makes the mask
  // truncate/garble it ("409800") — masks must be driven keystroke-by-keystroke. So for phone
  // fields we TYPE the digits one at a time, re-reading the (mask-reformatted) value between each,
  // which lets the mask build "(409) 800-4224" correctly. Plain fields just end up with the digits.
  function _isPhoneField(el) {
    const sig = ((el.name || "") + " " + (el.id || "") + " " + (el.getAttribute("placeholder") || "")
      + " " + (el.getAttribute("aria-label") || "")).toLowerCase();
    return el.type === "tel" || (/\bphone\b|mobile|tel\b|cell/.test(sig) && !/email/.test(sig));
  }
  function typePhone(el, value) {
    const digits = (value || "").replace(/\D/g, "");
    if (!digits) return;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    try { el.focus(); } catch (e) {}
    try { if (el._valueTracker) el._valueTracker.setValue("x"); } catch (e) {}
    setter.call(el, "");
    try { el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" })); } catch (e) { el.dispatchEvent(new Event("input", { bubbles: true })); }
    for (const d of digits) {
      const cur = el.value;                       // mask-formatted value so far
      try { if (el._valueTracker) el._valueTracker.setValue(cur); } catch (e) {}
      setter.call(el, cur + d);                   // append next digit; mask reformats on input
      el.dispatchEvent(new KeyboardEvent("keydown", { key: d, keyCode: 48 + Number(d), which: 48 + Number(d), bubbles: true }));
      try { el.dispatchEvent(new InputEvent("input", { bubbles: true, data: d, inputType: "insertText" })); } catch (e) { el.dispatchEvent(new Event("input", { bubbles: true })); }
      el.dispatchEvent(new KeyboardEvent("keyup", { key: d, bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function setNative(el, value) {
    if (_isPhoneField(el) && /\d/.test(value || "")) { typePhone(el, value); return; }
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    const fire = (data, inputType) => {
      try { el.dispatchEvent(new InputEvent("input", { bubbles: true, data: data == null ? null : String(data), inputType })); }
      catch (e) { el.dispatchEvent(new Event("input", { bubbles: true })); }
    };
    try { el.focus(); } catch (e) {}
    // CLEAR FIRST: Ashby / react-hook-form validate their OWN state, not the DOM. Setting the
    // value in one shot sometimes doesn't register (form submits as "Missing required field"
    // though the box visibly shows the value). Forcing an empty→value transition — reset the
    // valueTracker, set "", fire a delete, then set the real value and fire an insert — makes
    // React's change detector see a genuine edit and store it.
    try { if (el._valueTracker) el._valueTracker.setValue("x"); } catch (e) {}
    setter.call(el, "");
    fire(null, "deleteContentBackward");
    try { if (el._valueTracker) el._valueTracker.setValue(""); } catch (e) {}
    setter.call(el, value);
    fire(value, "insertText");
    el.dispatchEvent(new Event("change", { bubbles: true }));
    // keyup nudges frameworks that listen for keystrokes; blur marks the field touched so the
    // "required" validation re-runs against the new value and clears the error.
    try { el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "a" })); } catch (e) {}
    try { el.dispatchEvent(new Event("blur", { bubbles: true })); } catch (e) {}
  }

  try {
  // ── Wait for the form to render before scraping ──────────────────────────────
  // The page may still be loading / redirecting / hydrating (a React SPA) when we're injected —
  // especially in a BACKGROUND tab opened by "Fill ALL", which Chrome loads lazily. Without this,
  // we'd scrape 0 fields and bail, and you'd have to re-trigger. Poll until real form controls
  // exist (or ~25s). Also wait a beat for late-rendering fields once the first ones appear.
  const _formSel = "input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, " +
    "select, [class*='select__control'], [class*='__control'], [role='radio'], [role='combobox']";
  {
    const start = Date.now();
    let seen = 0;
    while (Date.now() - start < 25000) {
      const n = document.querySelectorAll(_formSel).length;
      // Stable for two checks (count stopped growing) → the form has finished rendering.
      if (n > 0 && n === seen) break;
      seen = n;
      await sleep(600);
    }
  }

  // ── Scrape fields ────────────────────────────────────────────────────────────
  const fields = [];      // {id, label, type, options}
  const reg = {};         // id -> {el / radios / control, kind}
  let uid = 0;
  const nid = () => "ah" + ++uid;

  // Label for a plain input/select, with fallbacks for forms that use a PLACEHOLDER or just a
  // name/id instead of a <label> — Kforce / TEKsystems / Insight Global custom apply forms do this,
  // so labelFor returns nothing and every field gets skipped (the "Filled 0" bug). clean() strips
  // the required '*'. placeholder "First Name *" → "First Name"; name "postalCode" → "postal code".
  // Generic page text labelFor sometimes grabs off a custom form (Kforce labeled its State select
  // "All fields with an asterisk are required"). Reject these so we fall through to the real signal.
  const _BAD_LABEL = /all fields|are required|asterisk|indicates?\s+required|required field|please\s+(complete|fill)|fields with/i;
  function fieldLabel(el) {
    let l = labelFor(el);
    if (l && !_BAD_LABEL.test(l)) return l;
    l = clean(el.getAttribute("placeholder") || "") || clean(el.getAttribute("aria-label") || "")
        || clean(el.getAttribute("title") || "");
    if (l && !_BAD_LABEL.test(l)) return l;
    if (el.tagName === "SELECT" && el.options && el.options.length) {
      const ph = clean(el.options[0].textContent || "");   // e.g. "Select State:" → "State"
      if (ph) { const s = ph.replace(/^\s*(select|choose|please select)\s+/i, "").replace(/[:*]+\s*$/, "").trim(); if (s) return s; }
    }
    const raw = (el.getAttribute("name") || el.id || "")    // "firstName" → "first name", "state" → "state"
      .replace(/[_\-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
    return clean(raw).toLowerCase();
  }

  // text-like inputs + textareas
  const TEXT_TYPES = ["text", "email", "tel", "url", "number", ""];
  for (const el of document.querySelectorAll("input, textarea")) {
    if (!visible(el)) continue;
    const t = (el.getAttribute("type") || "").toLowerCase();
    if (el.tagName === "INPUT" && !TEXT_TYPES.includes(t)) continue; // skip file/checkbox/radio/etc here
    if (el.disabled || el.readOnly) continue;
    if (el.value && el.value.trim()) continue; // already filled
    // Skip a react-select's inner <input> — it's handled by the react-select pass below.
    // Treating it as a text field makes us TYPE the answer ("Man") into the combobox instead
    // of PICKING the option ("Male"), which filters to "No options" and hangs the menu open.
    const ctrlAnc = el.closest("[class*='select__control'], [class*='__control']");
    if ((ctrlAnc && isReactSelect(ctrlAnc)) || el.getAttribute("role") === "combobox"
        || el.getAttribute("aria-autocomplete") === "list"
        || /(^|-)react-select-/.test(el.id || "")) continue;
    // Skip obvious site-search boxes (placeholder/name "search") — never an application field.
    if (/\bsearch\b/i.test((el.getAttribute("placeholder") || "") + " " + (el.name || el.id || ""))) continue;
    const label = fieldLabel(el);
    if (!label) continue;
    const id = nid();
    el.dataset.ahId = id;
    reg[id] = { el, kind: "text" };
    fields.push({ id, label, type: el.tagName === "TEXTAREA" ? "textarea" : (t || "text"), options: [] });
  }

  // native selects
  for (const el of document.querySelectorAll("select")) {
    if (!visible(el) || el.disabled) continue;
    if (el.value && el.value.trim() && !["", "0", "-1"].includes(el.value)) continue;
    const label = fieldLabel(el);
    if (!label) continue;
    const id = nid();
    reg[id] = { el, kind: "select" };
    const options = [...el.options].map((o) => clean(o.textContent)).filter(Boolean);
    fields.push({ id, label, type: "select", options });
  }

  // radio groups (by name)
  const groups = {};
  for (const el of document.querySelectorAll("input[type='radio']")) {
    if (!ctrlPresent(el)) continue;  // native radio may be hidden behind a styled control
    (groups[el.name || ("__" + nid())] ||= []).push(el);
  }
  for (const [name, radios] of Object.entries(groups)) {
    // DON'T skip a group just because one radio is pre-checked: forms DEFAULT a wrong option
    // (Kforce defaults "authorized for any employer" — we must override with "require sponsorship").
    // Track the pre-checked state so an unrecognized default is kept (not flagged), but a recognized
    // group (work-auth/EEO) gets the truthful answer applied over the default.
    const prechecked = radios.some((r) => r.checked);
    const optPairs = radios.map((r) => ({ r, l: optionText(r) }));
    // the QUESTION (not the first option's text) → name fallback ("eligibility") if label is
    // missing/generic, so the resolver can still route it.
    let label = questionLabel(radios) || labelFor(radios[0]);
    if ((!label || _BAD_LABEL.test(label)) && name && !name.startsWith("__"))
      label = name.replace(/[_\-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
    if (!label || optPairs.every((o) => !o.l)) continue;
    const id = nid();
    reg[id] = { kind: "radio", pairs: optPairs, prechecked };
    fields.push({ id, label, type: "radio", options: optPairs.map((o) => o.l) });
  }

  // custom radio groups (role=radiogroup / role=radio — Workday, some Ashby/Lever themes)
  for (const grp of document.querySelectorAll("[role='radiogroup']")) {
    if (!visible(grp)) continue;
    if (grp.querySelector("input[type='radio']")) continue; // native — already handled above
    const radios = [...grp.querySelectorAll("[role='radio']")].filter(visible);
    if (!radios.length || radios.some((r) => r.getAttribute("aria-checked") === "true")) continue;
    let label = labelFor(grp);
    const lg = grp.querySelector("legend"); if (!label && lg) label = clean(lg.textContent);
    const optPairs = radios.map((r) => ({ r, l: clean(r.textContent) || clean(r.getAttribute("aria-label") || "") }));
    if (!label || optPairs.every((o) => !o.l)) continue;
    const id = nid();
    reg[id] = { kind: "radio", pairs: optPairs };
    fields.push({ id, label, type: "radio", options: optPairs.map((o) => o.l) });
  }

  // checkbox groups ("select all that apply"). Ashby gives each checkbox NO shared name
  // and NO fieldset, so we group by the QUESTION they roll up to (questionLabel walks to
  // the shared field wrapper). All "sexual orientation" boxes → one group, etc.
  const cbByQ = new Map();
  for (const el of document.querySelectorAll("input[type='checkbox']")) {
    if (!ctrlPresent(el) || el.disabled || el.checked) continue;
    const q = questionLabel([el]) || labelFor(el) || optionText(el);
    if (!q) continue;
    if (!cbByQ.has(q)) cbByQ.set(q, []);
    cbByQ.get(q).push(el);
  }
  for (const [q, boxes] of cbByQ) {
    const optPairs = boxes.map((c) => ({ r: c, l: optionText(c) }));
    if (optPairs.every((o) => !o.l)) continue;
    const id = nid();
    reg[id] = { kind: "checkbox", pairs: optPairs };
    fields.push({ id, label: q, type: "checkbox", options: optPairs.map((o) => o.l).filter(Boolean) });
  }

  // React-Select controls (open each once to read its options)
  const rsControls = [...document.querySelectorAll("[class*='select__control'], [class*='__control']")]
    .filter((el) => visible(el) && isReactSelect(el) && !el.querySelector("select"));
  for (const ctrl of rsControls) {
    if (ctrl.querySelector(".select__single-value, [class*='__single-value']")) continue; // already chosen
    const inpEl = ctrl.querySelector("input");
    const label = reactSelectLabel(ctrl, inpEl);
    if (!label) continue;
    const options = await readReactOptions(ctrl);
    const id = nid();
    reg[id] = { kind: "react", control: ctrl };
    fields.push({ id, label, type: "select", options });
  }

  // With allFrames injection, most frames have no form — stay silent there so we don't
  // litter empty panels. Only the frame holding the form proceeds.
  if (!fields.length) return;

  // ── De-duplicate by label ────────────────────────────────────────────────────
  // New-style Greenhouse (job-boards.greenhouse.io) scrapes the SAME question twice: once as the
  // react-select's inner <input> (kind text) and once as the real react-select (kind select). The
  // text twin then gets the wrong treatment (an essay LLM draft on a Yes/No dropdown) and can't
  // commit. For each label, keep the highest-rank widget (select/react > text > checkbox) and drop
  // the rest — but keep MULTIPLE same-rank fields (two distinct "If you answered Yes…" texts).
  const rankOf = (f) => f.type === "select" ? 3 : f.type === "checkbox" ? 1 : 2;
  const byLabel = new Map();
  for (const f of fields) {
    const k = clean(f.label).toLowerCase();
    if (!byLabel.has(k)) byLabel.set(k, []);
    byLabel.get(k).push(f);
  }
  const dropIds = new Set();
  for (const grp of byLabel.values()) {
    if (grp.length < 2) continue;
    const max = Math.max(...grp.map(rankOf));
    for (const f of grp) if (rankOf(f) < max) dropIds.add(f.id);
  }
  const sendFields = fields.filter((f) => !dropIds.has(f.id));

  // ── Ask the backend ──────────────────────────────────────────────────────────
  const resp = await chrome.runtime.sendMessage({
    type: "resolve", url: location.href, referrer: document.referrer, fields: sendFields,
  });
  if (!resp || !resp.ok) {
    showOverlay({ error: (resp && resp.error) || "Backend unreachable — is the API running on :8000?", summary: {} }, []);
    return;
  }
  const data = resp.data;

  // ── Apply fills ────────────────────────────────────────────────────────────────
  const notes = [];
  const blanks = [];
  const attempts = [];    // {item, fld, fill} — every value fill; VERIFIED against the live DOM below
  const skips = [];       // {item, fld, note} — resolver had no confident answer
  const textFills = [];   // {el, value} — re-asserted after a settle delay (Ashby state-drop fix)
  for (const fill of data.fills || []) {
    const item = reg[fill.id];
    if (!item) continue;
    const fld = fields.find((f) => f.id === fill.id) || {};
    if (fill.kind === "blank") {
      // Deliberately left empty (e.g. a conditional that doesn't apply) — no action.
      blanks.push({ label: fld.label, note: fill.note });
      continue;
    }
    if (fill.kind === "skip") {
      // A pre-checked radio group the resolver doesn't recognize = a form DEFAULT that's fine
      // (e.g. résumé-upload "From this computer") — keep it, don't flag the user.
      if (item.prechecked) { markFilled(item); continue; }
      markNeedsYou(item, fld.label, fill.note);
      notes.push({ label: fld.label, note: fill.note || "needs you", status: "skip" });
      skips.push({ item, fld, note: fill.note });
      continue;
    }
    try { await applyFill(item, fill); } catch (e) {}
    if (item.kind === "text") textFills.push({ el: item.el, value: fill.value });
    attempts.push({ item, fld, fill });
  }
  // ── Re-assert text fields as the form settles ────────────────────────────────
  // Ashby (react-hook-form) drops a programmatically-set value during a post-mount RE-RENDER: the
  // input still SHOWS the text but the form's own state is empty, so submit fails "Missing required
  // field: Full Name/Phone" even though the box is green. Ashby re-renders MORE THAN ONCE
  // (hydration, validation, and again when the résumé attaches), so a single re-assert isn't enough
  // — re-fire the value at several points, AND once more after the résumé/cover attach below.
  const reassertText = () => {
    for (const { el, value } of textFills) {
      try { if (el.isConnected) setNative(el, value); } catch (e) {}
    }
  };
  if (textFills.length) {
    for (const gap of [400, 800, 1300]) { await sleep(gap); reassertText(); }
  }

  // ── Verify every fill against the live DOM, retry failures, recount from reality ──
  // The backend's summary counts what it RESOLVED, not what actually COMMITTED — which is how a
  // dropdown could silently fail while the panel proudly said "Needs you 0". Re-check each field
  // for real, retry the ones that didn't take, then mark + count from what's actually in the DOM.
  await sleep(250);
  const failures = [];
  for (const a of attempts) {
    if (!verifyFilled(a.item)) {
      try { await applyFill(a.item, a.fill); } catch (e) {}
      await sleep(150);
    }
    if (verifyFilled(a.item)) { markFilled(a.item); a.committed = true; }
    else {
      a.committed = false;
      markNeedsYou(a.item, a.fld.label, "couldn't confirm — recommended: " + a.fill.value);
      notes.push({ label: a.fld.label, note: "verify/pick: " + a.fill.value, status: "manual" });
      failures.push(a);
    }
  }
  // Recompute the summary from what ACTUALLY committed (overrides the backend's optimistic count).
  data.summary = {
    total: attempts.length + skips.length,
    filled: attempts.filter((a) => a.committed).length,
    needs_you: failures.length + skips.length,
    blank: blanks.length,
  };
  // ── Telemetry: report the VERIFIED per-field outcome so we fix CLASSES of forms from data ──
  // (labels / widget kinds / reasons only — NEVER the user's values). Failures + resolver-skips
  // reveal which widgets and labels still need work, instead of debugging one screenshot at a time.
  try {
    const reportFields = [
      ...failures.map((a) => ({ label: a.fld.label || "", kind: a.item.kind, type: a.fld.type || "",
        options_count: (a.fld.options || []).length, value_kind: a.fill.kind || "value",
        committed: false, reason: (a.fill.note || "did not commit").slice(0, 140) })),
      ...skips.map((s) => ({ label: s.fld.label || "", kind: s.item.kind, type: s.fld.type || "",
        options_count: (s.fld.options || []).length, value_kind: "skip",
        committed: false, reason: ("resolver: " + (s.note || "no rule")).slice(0, 140) })),
    ];
    chrome.runtime.sendMessage({
      type: "report",
      report: { url: location.href, host: location.host, job: data.job || null,
        total: data.summary.total, filled: data.summary.filled, needs_you: data.summary.needs_you,
        fields: reportFields },
    });
  } catch (e) {}

  // ── Attach the tailored resume to the form's file input ──────────────────────
  // A content script can't set a file path, but it CAN attach a real File via
  // DataTransfer. We fetch the per-job tailored PDF from the backend and attach it.
  let resumeStatus = null;
  let resumeViewUrl = null;
  let coverInput = null;   // hoisted: the cover-letter attach block below also needs it
  try {
    const fileInputs = [...document.querySelectorAll("input[type='file']")];
    const fieldText = (el) => ((el.name || "") + " " + (el.id || "") + " " +
      (el.getAttribute("aria-label") || "") + " " + (labelFor(el) || "") + " " +
      (el.accept || "")).toLowerCase();
    const looksCover = (el) => /cover.?letter|coverletter|motivation/.test(fieldText(el));
    const looksResume = (el) => /resume|cv|curriculum/.test(fieldText(el));
    const acceptsDoc = (el) => {
      const a = (el.accept || "").toLowerCase();
      return a === "" || a.includes("pdf") || a.includes("doc");
    };
    coverInput = fileInputs.find(looksCover);
    // Never let the résumé land in the cover-letter slot (the acceptsDoc fallback could).
    const resumeCandidates = fileInputs.filter((el) => el !== coverInput && !looksCover(el));
    const resumeInput = resumeCandidates.find(looksResume) || resumeCandidates.find(acceptsDoc);
    if (resumeInput && !(resumeInput.files && resumeInput.files.length)) {
      let r = await chrome.runtime.sendMessage({
        type: "resume", url: location.href, referrer: document.referrer,
      });
      // Tailor-on-demand: opening this form told the backend to start tailoring (data.tailoring).
      // The résumé isn't ready instantly (~20-30s of Opus) — poll until it lands, then attach.
      if ((!r || !r.ok) && data.tailoring) {
        showOverlay(data, notes, "⏳ Tailoring your résumé for this role… (~20s)", null, blanks);
        for (let i = 0; i < 20 && (!r || !r.ok); i++) {
          await sleep(2500);
          r = await chrome.runtime.sendMessage({
            type: "resume", url: location.href, referrer: document.referrer,
          });
        }
      }
      if (r && r.ok && r.base64) {
        const bytes = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
        const file = new File([bytes], r.filename || "resume.pdf", { type: "application/pdf" });

        // (a) Standard path: set the input's files + fire change (works for plain inputs).
        const dt1 = new DataTransfer(); dt1.items.add(file);
        try { resumeInput.files = dt1.files; } catch (e) {}
        resumeInput.dispatchEvent(new Event("input", { bubbles: true }));
        resumeInput.dispatchEvent(new Event("change", { bubbles: true }));

        // (b) Drag-and-drop path: Ashby/Greenhouse use react-dropzone, which listens for a
        // DROP event with dataTransfer.files — not the input's change. Fire the full sequence
        // on the dropzone (the element showing "drag and drop").
        if (!(resumeInput.files && resumeInput.files.length) ||
            /drag|drop/i.test((resumeInput.closest("[class*='dropzone' i],[class*='upload' i]") || {}).textContent || "")) {
          let zone = resumeInput.closest("[class*='dropzone' i],[class*='upload' i],[class*='drop' i]");
          if (!zone) {
            zone = [...document.querySelectorAll("div,section")]
              .find((d) => /drag and drop|drop here|upload file/i.test(d.textContent || "") &&
                           d.querySelector("input[type='file']"));
          }
          if (zone) {
            const dt2 = new DataTransfer(); dt2.items.add(file);
            for (const type of ["dragenter", "dragover", "drop"]) {
              let ev;
              try { ev = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt2 }); }
              catch (e) {
                ev = new DragEvent(type, { bubbles: true, cancelable: true });
                try { Object.defineProperty(ev, "dataTransfer", { value: dt2 }); } catch (_) {}
              }
              zone.dispatchEvent(ev);
            }
          }
        }

        await sleep(400);
        // AUTHORITATIVE signal: the input actually holds a file. Text-sniffing (the word "resume"
        // in a label) caused false "✅ attached" → users submitted résumé-less. Only trust files;
        // when files isn't observable (some dropzones), report "likely — verify", never a green ✅.
        const hasFile = !!(resumeInput.files && resumeInput.files.length);
        const textHint = /\.pdf|\.docx?|attached|uploaded/i.test(
          (resumeInput.closest("[class*='field' i],[class*='resume' i],[class*='upload' i],form") || document.body).textContent || "");
        const box = resumeInput.closest("[class*='field'],[class*='resume'],fieldset,div") || resumeInput;
        if (box && box.style) { box.style.outline = "2px solid " + (hasFile ? "#22c55e" : "#eab308"); box.style.outlineOffset = "1px"; }
        resumeStatus = hasFile
          ? "✅ Tailored resume attached — " + (r.filename || "resume.pdf")
          : textHint
            ? "⚠️ Résumé may be attached — PLEASE VERIFY before submitting (auto-attach couldn't confirm)"
            : "✋ Couldn't auto-attach résumé on this uploader — click 'Upload File' and pick it (opens via 'View attached')";
        resumeViewUrl = "http://localhost:8000/api/v1/autofill/resume?url=" +
          encodeURIComponent(location.href) + "&referrer=" + encodeURIComponent(document.referrer);
      } else {
        resumeStatus = data.tailoring
          ? "⏳ Résumé still tailoring — wait a few seconds and re-click the extension to attach it"
          : (r && r.status === 404)
            ? "✋ No tailored resume matched this job — attach manually"
            : "✋ Couldn't fetch resume (is the backend running?)";
      }
    } else if (resumeInput) {
      resumeStatus = "Resume already attached on the page";
    }
  } catch (e) {
    resumeStatus = "✋ Resume attach failed: " + (e && e.message ? e.message : e);
  }

  // ── Cover letter: attach PDF to a file input, or confirm the text-field fill ──
  // Always expose a "View cover letter" link when this job has one (data.has_cover), so you can
  // see exactly what's being attached/filled — regardless of field type.
  let coverStatus = null;
  let coverViewUrl = data.has_cover
    ? "http://localhost:8000/api/v1/autofill/cover?url=" +
        encodeURIComponent(location.href) + "&referrer=" + encodeURIComponent(document.referrer)
    : null;
  try {
    // On-demand cover letter — the form HAS a cover-letter upload but this role has none yet (the
    // pipeline only pre-writes one for some roles). The field itself is the strongest signal one is
    // wanted, so generate it now (mirrors the résumé on-demand flow), then attach below.
    if (coverInput && !(coverInput.files && coverInput.files.length) && !data.has_cover) {
      showOverlay(data, notes, resumeStatus, resumeViewUrl, blanks,
                  "⏳ Writing a cover letter for this role… (~15s)", null);
      try {
        const g = await chrome.runtime.sendMessage({
          type: "cover_generate", url: location.href, referrer: document.referrer,
        });
        if (g && g.ok && g.data && (g.data.status === "generated" || g.data.status === "exists")) {
          data.has_cover = true;
          coverViewUrl = "http://localhost:8000/api/v1/autofill/cover?url=" +
            encodeURIComponent(location.href) + "&referrer=" + encodeURIComponent(document.referrer);
        }
      } catch (e) {}
    }
    if (coverInput && !(coverInput.files && coverInput.files.length)) {
      const r = await chrome.runtime.sendMessage({
        type: "cover", url: location.href, referrer: document.referrer,
      });
      if (r && r.ok && r.base64) {
        const bytes = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
        const file = new File([bytes], r.filename || "cover_letter.pdf", { type: "application/pdf" });
        const dt = new DataTransfer(); dt.items.add(file);
        try { coverInput.files = dt.files; } catch (e) {}
        coverInput.dispatchEvent(new Event("input", { bubbles: true }));
        coverInput.dispatchEvent(new Event("change", { bubbles: true }));
        if (!(coverInput.files && coverInput.files.length)) {
          const zone = coverInput.closest("[class*='dropzone' i],[class*='upload' i],[class*='drop' i]");
          if (zone) {
            const dt2 = new DataTransfer(); dt2.items.add(file);
            for (const type of ["dragenter", "dragover", "drop"]) {
              let ev;
              try { ev = new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt2 }); }
              catch (e) {
                ev = new DragEvent(type, { bubbles: true, cancelable: true });
                try { Object.defineProperty(ev, "dataTransfer", { value: dt2 }); } catch (_) {}
              }
              zone.dispatchEvent(ev);
            }
          }
        }
        await sleep(350);
        const hasFile = !!(coverInput.files && coverInput.files.length);
        const box = coverInput.closest("[class*='field'],[class*='cover'],fieldset,div") || coverInput;
        if (box && box.style) { box.style.outline = "2px solid " + (hasFile ? "#22c55e" : "#eab308"); box.style.outlineOffset = "1px"; }
        coverStatus = hasFile
          ? "✅ Cover letter attached — " + (r.filename || "cover_letter.pdf")
          : "⚠️ Cover letter may not have attached — verify (open via link)";
      } else if (r && r.status === 404) {
        coverStatus = "ℹ️ No tailored cover letter for this role — write one or skip";
        coverViewUrl = null;
      }
    } else if (data.has_cover) {
      // No cover-letter FILE input (it's a text field we already filled, or none on this form).
      coverStatus = "✅ Cover letter ready — filled / view to copy";
    }
  } catch (e) {
    coverStatus = "✋ Cover letter attach failed: " + (e && e.message ? e.message : e);
  }

  // Final re-assert (universal): attaching the résumé/cover triggers one more Ashby re-render that
  // can re-drop fields. Re-fire ALL value fills — text via setNative (always; idempotent), and
  // select/radio/react ONLY if they're now visibly EMPTY (so we never toggle a multi-select OFF or
  // disturb a still-committed dropdown — that's the safe boundary). A select whose chip still shows
  // but whose RHF state silently dropped is undetectable here; a re-click of Fill recovers it.
  if (attempts.length) {
    await sleep(400);
    reassertText();
    for (const a of attempts) {
      if (a.item.kind === "text") continue;
      try { if (!verifyFilled(a.item)) await applyFill(a.item, a.fill); } catch (e) {}
    }
  }

  showOverlay(data, notes, resumeStatus, resumeViewUrl, blanks, coverStatus, coverViewUrl);
  _ahDone();
  } catch (err) {
    _ahDone();
    try {
      if (document.body) {
        const b = document.createElement("div");
        b.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;max-width:340px;" +
          "background:#7f1d1d;color:#fee2e2;font:12px -apple-system,sans-serif;padding:12px 14px;" +
          "border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.5)";
        b.textContent = "AutonomousHire autofill error: " + (err && err.message ? err.message : err);
        document.body.appendChild(b);
      }
    } catch (_) {}
    console.error("[AutonomousHire] autofill error:", err);
  }

  // Apply ONE resolved fill to its widget — shared by the first pass and the verify-retry pass
  // (single source of truth for how each widget kind is filled).
  async function applyFill(item, fill) {
    if (item.kind === "text") { setNative(item.el, fill.value); return true; }
    if (item.kind === "select") { return selectNative(item.el, fill.value); }
    if (item.kind === "radio" || item.kind === "checkbox") {
      // The backend returns the EXACT option text it chose. Match precisely: exact →
      // option-startswith-want → option-contains-want. NEVER want-contains-option (reverse) —
      // "Asian (Not Hispanic or Latino)" contains "Hispanic or Latino", which would wrongly
      // tick the Hispanic radio. Protected-class data must be exact.
      const want = clean(fill.value).toLowerCase();
      const m = item.pairs.find((p) => clean(p.l).toLowerCase() === want)
        || item.pairs.find((p) => { const pl = clean(p.l).toLowerCase();
             return pl.length > 2 && (pl.startsWith(want + " ") || pl.startsWith(want + ",") || pl.startsWith(want + " (")); })
        || item.pairs.find((p) => { const pl = clean(p.l).toLowerCase();
             return want.length > 3 && pl.includes(want); });
      if (!m) return false;
      if (item.kind === "checkbox" && m.r.checked) return true;
      m.r.click();
      // Styled control: if clicking the hidden native input didn't take, click its label.
      if (m.r.checked === false) {
        const lab = m.r.closest("label") || (m.r.id && document.querySelector(`label[for="${CSS.escape(m.r.id)}"]`));
        if (lab) lab.click();
      }
      return true;
    }
    if (item.kind === "react") { return await fillReactSelect(item.control, fill.value); }
    return false;
  }

  // Did the fill actually land in the live DOM? (Not "did we dispatch events" — the real value.)
  function verifyFilled(item) {
    try {
      if (item.kind === "text") return !!clean(item.el.value);
      if (item.kind === "select") return !!(item.el.value && !["", "0", "-1"].includes(item.el.value));
      if (item.kind === "radio" || item.kind === "checkbox") return item.pairs.some((p) => p.r.checked);
      if (item.kind === "react") return committed(item.control);
    } catch (e) {}
    return false;
  }

  function selectNative(el, value) {
    const v = clean(value).toLowerCase();
    for (const o of el.options) {
      if (clean(o.textContent).toLowerCase() === v || (o.value || "").toLowerCase() === v) {
        el.value = o.value; el.dispatchEvent(new Event("change", { bubbles: true })); return true;
      }
    }
    for (const o of el.options) {
      if (clean(o.textContent).toLowerCase().includes(v)) {
        el.value = o.value; el.dispatchEvent(new Event("change", { bubbles: true })); return true;
      }
    }
    return false;
  }

  // ── Visual feedback ──────────────────────────────────────────────────────────
  function targetEl(item) {
    return (item.kind === "radio" || item.kind === "checkbox")
        ? (item.pairs[0] && (item.pairs[0].r.closest("fieldset, [role='group'], [role='radiogroup']") || item.pairs[0].r))
      : item.kind === "react" ? item.control : item.el;
  }
  function markFilled(item) {
    const el = targetEl(item); if (!el) return;
    el.style.outline = "2px solid #22c55e"; el.style.outlineOffset = "1px";
  }
  function markNeedsYou(item, label, note) {
    const el = targetEl(item); if (!el) return;
    el.style.outline = "2px solid #eab308"; el.style.outlineOffset = "1px";
    el.title = "AutonomousHire: " + (note || "please complete");
  }

  function showOverlay(data, notes, resumeStatus, resumeViewUrl, blanks, coverStatus, coverViewUrl) {
    document.getElementById("ah-overlay")?.remove();
    document.getElementById("__ah_badge")?.remove();
    const s = data.summary || {};
    const box = document.createElement("div");
    box.id = "ah-overlay";
    box.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;width:320px;" +
      "background:#0a1628;color:#e2e8f0;font:13px/1.4 -apple-system,Segoe UI,sans-serif;" +
      "border:1px solid #334155;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.5);padding:14px 16px;";
    const job = data.job ? `${data.job.company} — ${data.job.title}` : "Application form";
    let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong style="color:#fff">🤖 Autofill</strong>
        <span style="cursor:pointer;color:#64748b" onclick="this.closest('#ah-overlay').remove()">✕</span></div>
      <div style="color:#94a3b8;font-size:12px;margin-bottom:10px">${job}</div>`;
    if (data.error) {
      html += `<div style="color:#f87171">${data.error}</div>`;
    } else {
      html += `<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">
        <span style="color:#22c55e">✅ Filled ${s.filled || 0}</span>
        <span style="color:#eab308">✋ Needs you ${s.needs_you || 0}</span>` +
        ((s.blank || 0) ? `<span style="color:#64748b">⊘ Blank ${s.blank} (on purpose)</span>` : "") +
        `</div>`;
      if (resumeStatus) {
        const c = resumeStatus.startsWith("✅") ? "#22c55e" : "#eab308";
        const link = resumeViewUrl
          ? ` <a href="${resumeViewUrl}" target="_blank" style="color:#60a5fa;text-decoration:underline">View attached resume ↗</a>`
          : "";
        html += `<div style="color:${c};font-size:12px;margin-bottom:6px">${resumeStatus}${link}</div>`;
      }
      if (coverStatus || coverViewUrl) {
        const cc = (coverStatus || "").startsWith("✅") ? "#22c55e"
          : (coverStatus || "").startsWith("ℹ️") ? "#94a3b8" : "#eab308";
        const clink = coverViewUrl
          ? ` <a href="${coverViewUrl}" target="_blank" style="color:#60a5fa;text-decoration:underline">View cover letter ↗</a>`
          : "";
        html += `<div style="color:${cc};font-size:12px;margin-bottom:10px">${coverStatus || "Cover letter"}${clink}</div>`;
      }
      if (notes.length) {
        html += `<div style="border-top:1px solid #1e293b;padding-top:8px;max-height:240px;overflow:auto">`;
        for (const n of notes) {
          const c = n.status === "manual" ? "#fbbf24" : "#94a3b8";
          html += `<div style="margin:6px 0"><div style="color:#cbd5e1">${(n.label||"").slice(0,60)}</div>
            <div style="color:${c};font-size:11px">${(n.note||"").slice(0,80)}</div></div>`;
        }
        html += `</div>`;
      }
      if (blanks && blanks.length) {
        html += `<div style="border-top:1px solid #1e293b;padding-top:8px">
          <div style="color:#64748b;font-size:11px;margin-bottom:4px">⊘ Left blank on purpose (no action):</div>`;
        for (const b of blanks)
          html += `<div style="color:#475569;font-size:11px;margin:3px 0">• ${(b.label||"").slice(0,70)}</div>`;
        html += `</div>`;
      }
      html += `<div style="border-top:1px solid #1e293b;margin-top:8px;padding-top:8px;color:#64748b;font-size:11px">
        Review the highlighted fields, solve any CAPTCHA, then submit the form yourself.</div>`;
    }
    box.innerHTML = html;
    document.body.appendChild(box);
  }
})();
