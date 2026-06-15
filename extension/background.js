// Service worker: the content script can't call http://localhost from the page's
// origin (CORS), so it delegates network calls here. With host_permissions for
// localhost, the extension fetch is allowed regardless of the page origin.
const BASE = "http://localhost:8000/api/v1/autofill";

function bufToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  return btoa(bin);
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "resolve") {
    fetch(`${BASE}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: msg.url || "", referrer: msg.referrer || "", fields: msg.fields || [] }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  if (msg && msg.type === "report") {
    // Fire-and-forget telemetry: verified per-field fill outcomes (no user values).
    fetch(`${BASE}/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.report || {}),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  if (msg && msg.type === "cover_generate") {
    // On-demand cover letter: the form has a cover field but this role has none yet.
    fetch(`${BASE}/cover/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: msg.url || "", referrer: msg.referrer || "" }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  if (msg && (msg.type === "resume" || msg.type === "cover")) {
    const qs = new URLSearchParams({ url: msg.url || "", referrer: msg.referrer || "" });
    const fallbackName = msg.type === "cover" ? "cover_letter.pdf" : "resume.pdf";
    fetch(`${BASE}/${msg.type}?${qs.toString()}`)
      .then(async (r) => {
        if (!r.ok) return sendResponse({ ok: false, status: r.status });
        const buf = await r.arrayBuffer();
        const cd = r.headers.get("content-disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        sendResponse({ ok: true, base64: bufToBase64(buf), filename: m ? m[1] : fallbackName });
      })
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
});
