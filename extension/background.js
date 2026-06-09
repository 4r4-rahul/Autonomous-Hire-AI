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

  if (msg && msg.type === "resume") {
    const qs = new URLSearchParams({ url: msg.url || "", referrer: msg.referrer || "" });
    fetch(`${BASE}/resume?${qs.toString()}`)
      .then(async (r) => {
        if (!r.ok) return sendResponse({ ok: false, status: r.status });
        const buf = await r.arrayBuffer();
        const cd = r.headers.get("content-disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        sendResponse({ ok: true, base64: bufToBase64(buf), filename: m ? m[1] : "resume.pdf" });
      })
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
});
