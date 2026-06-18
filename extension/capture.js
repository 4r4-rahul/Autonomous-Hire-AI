// DEBUG one-shot: serialize the form's field structure (tags/roles/classes/labels/outerHTML) and
// send it to the backend (data/ah_domdump.json) so bespoke ATS widgets can be reverse-engineered.
// Injected on demand by the popup's "Capture form DOM (debug)" button. STRUCTURE only (no values).
// Pierces SHADOW DOM (custom-element forms hide inputs in shadow roots — invisible to querySelectorAll,
// which is likely why the generic scraper sees 0 options). Skips empty frames so an about:blank
// sub-frame can't overwrite the real capture.
(() => {
  const clip = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n || 500);
  const SEL = [
    "input", "select", "textarea", "button",
    "[role=combobox]", "[role=radio]", "[role=checkbox]", "[role=listbox]", "[role=option]",
    "[class*=select i]", "[class*=dropdown i]", "[class*=combobox i]",
  ].join(",");

  // Walk the whole tree INCLUDING shadow roots, collecting matches + counting shadow hosts.
  let shadowHosts = 0;
  function deepCollect(root, matches) {
    try { root.querySelectorAll(SEL).forEach((el) => matches.add(el)); } catch (e) {}
    let all = [];
    try { all = root.querySelectorAll("*"); } catch (e) {}
    for (const el of all) {
      if (el.shadowRoot) { shadowHosts++; deepCollect(el.shadowRoot, matches); }
    }
  }
  const set = new Set();
  deepCollect(document, set);
  const els = [...set].slice(0, 200);

  const labelFor = (el) => {
    try {
      if (!el.id) return "";
      const root = el.getRootNode();
      const l = (root.querySelector && root.querySelector(`label[for="${CSS.escape(el.id)}"]`));
      return l ? clip(l.textContent, 140) : "";
    } catch (e) { return ""; }
  };
  const fields = els.map((el, i) => {
    let r = {}; try { r = el.getBoundingClientRect(); } catch (e) {}
    let lbldby = "";
    const lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) lbldby = lb.split(/\s+/).map((id) => {
      try { return (el.getRootNode().getElementById && el.getRootNode().getElementById(id) || {}).textContent || ""; }
      catch (e) { return ""; }
    }).join(" ");
    return {
      i, tag: el.tagName, type: (el.getAttribute && el.getAttribute("type")) || "",
      role: (el.getAttribute && el.getAttribute("role")) || "",
      id: el.id || "", name: (el.getAttribute && el.getAttribute("name")) || "",
      placeholder: (el.getAttribute && el.getAttribute("placeholder")) || "",
      ariaLabel: (el.getAttribute && el.getAttribute("aria-label")) || "",
      ariaLabelledby: clip(lbldby, 140),
      cls: clip(el.className && el.className.toString ? el.className.toString() : "", 180),
      labelFor: labelFor(el),
      inShadow: el.getRootNode && el.getRootNode() !== document,
      visible: !!(r.width || r.height),
      outer: clip(el.outerHTML, 600),
      parent: clip(el.parentElement ? el.parentElement.outerHTML : "", 1100),
    };
  });

  // Don't let an empty sub-frame overwrite the real capture.
  if (!fields.length) return;
  chrome.runtime.sendMessage(
    { type: "domdump", url: location.href, shadowHosts, fields },
    (resp) => {
      const b = document.createElement("div");
      b.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;background:#0f766e;" +
        "color:#d1fae5;font:12px -apple-system,sans-serif;padding:10px 14px;border-radius:8px;" +
        "box-shadow:0 8px 30px rgba(0,0,0,.5)";
      b.textContent = (resp && resp.ok)
        ? `Captured ${fields.length} fields (${shadowHosts} shadow hosts) → sent. You can close this.`
        : "Capture failed: " + ((resp && resp.error) || "backend unreachable");
      document.body && document.body.appendChild(b);
      setTimeout(() => b.remove(), 6000);
    });
})();
