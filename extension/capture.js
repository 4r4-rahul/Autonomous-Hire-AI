// DEBUG one-shot: serialize the form's field structure (tags/roles/classes/labels/outerHTML) and
// send it to the backend (data/ah_domdump.json) so bespoke ATS widgets can be reverse-engineered.
// Injected on demand by the popup's "Capture form DOM (debug)" button. Captures STRUCTURE only.
(() => {
  const clip = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n || 500);
  const SEL = [
    "input", "select", "textarea", "button",
    "[role=combobox]", "[role=radio]", "[role=checkbox]", "[role=listbox]", "[role=option]",
    "[class*=select i]", "[class*=dropdown i]", "[class*=combobox i]", "[class*=field i]",
  ].join(",");
  const els = [...document.querySelectorAll(SEL)].slice(0, 150);
  const labelFor = (el) => {
    if (!el.id) return "";
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    return l ? clip(l.textContent, 140) : "";
  };
  const fields = els.map((el, i) => {
    const r = el.getBoundingClientRect();
    let lbldby = "";
    const lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) lbldby = lb.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" ");
    return {
      i,
      tag: el.tagName,
      type: el.getAttribute && el.getAttribute("type") || "",
      role: el.getAttribute && el.getAttribute("role") || "",
      id: el.id || "",
      name: (el.getAttribute && el.getAttribute("name")) || "",
      placeholder: (el.getAttribute && el.getAttribute("placeholder")) || "",
      ariaLabel: (el.getAttribute && el.getAttribute("aria-label")) || "",
      ariaLabelledby: clip(lbldby, 140),
      cls: clip(el.className && el.className.toString ? el.className.toString() : "", 180),
      labelFor: labelFor(el),
      visible: !!(r.width || r.height),
      outer: clip(el.outerHTML, 600),
      parent: clip(el.parentElement ? el.parentElement.outerHTML : "", 1100),
    };
  });
  chrome.runtime.sendMessage({ type: "domdump", url: location.href, fields }, (resp) => {
    const b = document.createElement("div");
    b.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;background:#0f766e;" +
      "color:#d1fae5;font:12px -apple-system,sans-serif;padding:10px 14px;border-radius:8px;" +
      "box-shadow:0 8px 30px rgba(0,0,0,.5)";
    b.textContent = (resp && resp.ok)
      ? `Captured ${fields.length} fields → sent for analysis. You can close this.`
      : "Capture failed: " + ((resp && resp.error) || "backend unreachable");
    document.body && document.body.appendChild(b);
    setTimeout(() => b.remove(), 6000);
  });
})();
