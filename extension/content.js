// AutonomousHire autofill — runs on demand (injected by the popup) in the page the
// user already has open. Scrapes form fields, asks the local backend for answers, fills
// them, and shows an overlay of what was filled vs. what needs the user. No submission —
// the user reviews, solves any CAPTCHA, and submits natively.
(async () => {
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

  const committed = (control) =>
    !!control.querySelector(".select__single-value, [class*='__single-value']");

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
    return null;
  }
  function optionEls(control) {
    const menu = menuFor(control);
    if (!menu) return [];
    return [...menu.querySelectorAll("[role='option'], .select__option, [class*='__option']")];
  }

  async function readReactOptions(control) {
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
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
      return all.find((o) => clean(o.textContent) === v)
        || all.find((o) => { const t = clean(o.textContent); return t.length > 3 && t.includes(v); })
        || (v.length > 6 && all.find((o) => { const t = clean(o.textContent);
             return t.length > 6 && v.startsWith(t); }));
    };
    try {
      rsOpen(control);
      await sleep(400);
      const opt = pick();
      if (opt) {
        opt.scrollIntoView({ block: "nearest" });
        // React-Select selects on mousedown; dispatch the full sequence to be safe.
        for (const t of ["pointerdown", "mousedown", "mouseup", "click"])
          opt.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
        await sleep(180);
      }
      // Keyboard fallback: type to filter the list, then Enter selects the focused option.
      if (!committed(control) && inp) {
        rsOpen(control);
        await sleep(150);
        inp.focus();
        setInputValue(inp, v.slice(0, 18));
        await sleep(950);  // geosuggest/async option lists need time to return
        inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, which: 13, bubbles: true }));
        await sleep(180);
      }
      rsClose(control);
      return committed(control);
    } catch (e) {}
    return false;
  }

  // ── Native value setter (React-friendly) ─────────────────────────────────────
  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  try {
  // ── Scrape fields ────────────────────────────────────────────────────────────
  const fields = [];      // {id, label, type, options}
  const reg = {};         // id -> {el / radios / control, kind}
  let uid = 0;
  const nid = () => "ah" + ++uid;

  // text-like inputs + textareas
  const TEXT_TYPES = ["text", "email", "tel", "url", "number", ""];
  for (const el of document.querySelectorAll("input, textarea")) {
    if (!visible(el)) continue;
    const t = (el.getAttribute("type") || "").toLowerCase();
    if (el.tagName === "INPUT" && !TEXT_TYPES.includes(t)) continue; // skip file/checkbox/radio/etc here
    if (el.disabled || el.readOnly) continue;
    if (el.value && el.value.trim()) continue; // already filled
    const label = labelFor(el);
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
    const label = labelFor(el);
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
    if (radios.some((r) => r.checked)) continue;
    const optPairs = radios.map((r) => ({ r, l: optionText(r) }));
    // the QUESTION (not the first option's text)
    const label = questionLabel(radios) || labelFor(radios[0]);
    if (!label || optPairs.every((o) => !o.l)) continue;
    const id = nid();
    reg[id] = { kind: "radio", pairs: optPairs };
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
    const label = labelFor(ctrl);
    if (!label) continue;
    const options = await readReactOptions(ctrl);
    const id = nid();
    reg[id] = { kind: "react", control: ctrl };
    fields.push({ id, label, type: "select", options });
  }

  // With allFrames injection, most frames have no form — stay silent there so we don't
  // litter empty panels. Only the frame holding the form proceeds.
  if (!fields.length) return;

  // ── Ask the backend ──────────────────────────────────────────────────────────
  const resp = await chrome.runtime.sendMessage({
    type: "resolve", url: location.href, referrer: document.referrer, fields,
  });
  if (!resp || !resp.ok) {
    showOverlay({ error: (resp && resp.error) || "Backend unreachable — is the API running on :8000?", summary: {} }, []);
    return;
  }
  const data = resp.data;

  // ── Apply fills ────────────────────────────────────────────────────────────────
  const notes = [];
  const blanks = [];
  for (const fill of data.fills || []) {
    const item = reg[fill.id];
    if (!item) continue;
    const fld = fields.find((f) => f.id === fill.id) || {};
    try {
      if (fill.kind === "blank") {
        // Deliberately left empty (e.g. a conditional that doesn't apply) — no action.
        blanks.push({ label: fld.label, note: fill.note });
        continue;
      }
      if (fill.kind === "skip") {
        markNeedsYou(item, fld.label, fill.note);
        notes.push({ label: fld.label, note: fill.note || "needs you", status: "skip" });
        continue;
      }
      let ok = false;
      if (item.kind === "text") { setNative(item.el, fill.value); ok = true; }
      else if (item.kind === "select") { ok = selectNative(item.el, fill.value); }
      else if (item.kind === "radio" || item.kind === "checkbox") {
        // The backend returns the EXACT option text it chose. Match precisely:
        // exact → option-startswith-want → option-contains-want. NEVER want-contains-option
        // (reverse match) — "Asian (Not Hispanic or Latino)" contains "Hispanic or Latino",
        // which would wrongly tick the Hispanic radio. Protected-class data must be exact.
        const want = clean(fill.value).toLowerCase();
        const m = item.pairs.find((p) => clean(p.l).toLowerCase() === want)
          || item.pairs.find((p) => { const pl = clean(p.l).toLowerCase();
               return pl.length > 2 && (pl.startsWith(want + " ") || pl.startsWith(want + ",") || pl.startsWith(want + " ("));
             })
          || item.pairs.find((p) => { const pl = clean(p.l).toLowerCase();
               return want.length > 3 && pl.includes(want);
             });
        if (m) {
          if (item.kind === "checkbox" && m.r.checked) { ok = true; }
          else {
            m.r.click();
            // Styled control: if clicking the hidden native input didn't take, click its label.
            if (m.r.checked === false) {
              const lab = m.r.closest("label")
                || (m.r.id && document.querySelector(`label[for="${CSS.escape(m.r.id)}"]`));
              if (lab) lab.click();
            }
            ok = true;
          }
        }
      } else if (item.kind === "react") { ok = await fillReactSelect(item.control, fill.value); }

      if (ok) { markFilled(item); }
      else { markNeedsYou(item, fld.label, "couldn't set — recommended: " + fill.value);
        notes.push({ label: fld.label, note: "pick: " + fill.value, status: "manual" }); }
    } catch (e) {
      markNeedsYou(item, fld.label, "error filling");
    }
  }
  // ── Attach the tailored resume to the form's file input ──────────────────────
  // A content script can't set a file path, but it CAN attach a real File via
  // DataTransfer. We fetch the per-job tailored PDF from the backend and attach it.
  let resumeStatus = null;
  let resumeViewUrl = null;
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
    const coverInput = fileInputs.find(looksCover);
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

  // ── Attach the reviewed cover letter PDF to a cover-letter file input ─────────
  let coverStatus = null;
  try {
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
      }
    }
  } catch (e) {
    coverStatus = "✋ Cover letter attach failed: " + (e && e.message ? e.message : e);
  }

  const attachMsg = [resumeStatus, coverStatus].filter(Boolean).join("\n");
  showOverlay(data, notes, attachMsg, resumeViewUrl, blanks);
  } catch (err) {
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

  function showOverlay(data, notes, resumeStatus, resumeViewUrl, blanks) {
    document.getElementById("ah-overlay")?.remove();
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
        html += `<div style="color:${c};font-size:12px;margin-bottom:10px">${resumeStatus}${link}</div>`;
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
