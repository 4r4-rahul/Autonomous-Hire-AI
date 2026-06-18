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

// ── "Fill ALL" → fill each tab when you switch to it ─────────────────────────────────────────
// Chrome freezes timers in BACKGROUND tabs, so injecting the filler into them does little until
// they're focused. Instead "Fill ALL" arms the other application tabs here; each is filled the
// instant it becomes active (full speed, no throttle, loads if it was lazy/discarded). Armed IDs
// live in session storage so they survive the service worker being recycled.
async function _getArmed() {
  try { return new Set((await chrome.storage.session.get("armed")).armed || []); }
  catch (e) { return new Set(); }
}
async function _setArmed(set) {
  try { await chrome.storage.session.set({ armed: [...set] }); } catch (e) {}
}

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const armed = await _getArmed();
  if (!armed.has(tabId)) return;
  armed.delete(tabId);
  await _setArmed(armed);
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["content.js"] });
  } catch (e) { /* tab restricted / closed — skip */ }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "arm") {
    (async () => {
      const a = await _getArmed();
      (msg.tabIds || []).forEach((id) => a.add(id));
      await _setArmed(a);
      sendResponse({ ok: true, armed: a.size });
    })();
    return true;
  }

  if (msg && msg.type === "open_incognito") {
    // Reopen a URL in an INCOGNITO window — a fresh session clears cookie/fingerprint-based bot
    // blocks (PerimeterX on TEKsystems 403s a normal tab but lets an incognito one through). Reuse
    // an existing incognito window so repeated opens don't pile up new windows. Auto-fills once the
    // page loads. Requires the extension's "Allow in incognito" to be ON (else create() throws).
    (async () => {
      try {
        const wins = await chrome.windows.getAll({});
        const incog = wins.find((w) => w.incognito);
        let tabId;
        if (incog) {
          const t = await chrome.tabs.create({ windowId: incog.id, url: msg.url, active: true });
          tabId = t.id;
          await chrome.windows.update(incog.id, { focused: true });
        } else {
          const w = await chrome.windows.create({ url: msg.url, incognito: true, focused: true });
          tabId = w.tabs && w.tabs[0] && w.tabs[0].id;
        }
        if (msg.closeTabId) { try { await chrome.tabs.remove(msg.closeTabId); } catch (e) {} }
        if (tabId && msg.autofill !== false) {
          const onDone = (id, info) => {
            if (id === tabId && info.status === "complete") {
              chrome.tabs.onUpdated.removeListener(onDone);
              // small delay so the SPA renders its form before we scrape
              setTimeout(() => chrome.scripting.executeScript({
                target: { tabId, allFrames: true }, files: ["content.js"],
              }).catch(() => {}), 1800);
            }
          };
          chrome.tabs.onUpdated.addListener(onDone);
        }
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      }
    })();
    return true;
  }

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
