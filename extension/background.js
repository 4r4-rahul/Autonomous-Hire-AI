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

// The URL the extension OPENED a tab with (e.g. a job page reopened in incognito). Stored per
// tab so the backend can match the job even after an "Apply Now" navigation strips the job id /
// referrer from the apply-page URL. Returned as the `origin` matching hint on resolve/resume/cover.
async function _srcFor(sender) {
  try {
    const id = sender && sender.tab && sender.tab.id;
    if (id == null) return "";
    const direct = (await chrome.storage.session.get("src_" + id))["src_" + id];
    if (direct) return direct;
    // "Apply Now" frequently opens a NEW tab (often cross-domain, e.g. apply.teksystems.com) — walk
    // the opener-tab chain to find the job URL we stored on the originating tab.
    let curId = id;
    for (let i = 0; i < 4; i++) {
      let tab;
      try { tab = await chrome.tabs.get(curId); } catch (e) { break; }
      const opener = tab && tab.openerTabId;
      if (opener == null) break;
      const v = (await chrome.storage.session.get("src_" + opener))["src_" + opener];
      if (v) return v;
      curId = opener;
    }
    // Last resort: the most-recently incognito-opened job URL, if recent (handles Apply-Now tabs
    // opened with rel=noopener, which carry no openerTabId). Token-validated by _match_job anyway.
    const g = await chrome.storage.session.get(["last_src", "last_src_ts"]);
    if (g.last_src && g.last_src_ts && (DATE_NOW() - g.last_src_ts) < 600000) return g.last_src;
    return "";
  } catch (e) { return ""; }
}
// Date.now() is fine in the extension (only the workflow sandbox forbids it).
function DATE_NOW() { return Date.now(); }

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

  if (msg && msg.type === "open_tabs") {
    // Open a batch of apply URLs as background tabs (no pop-up blocker, unlike the page's
    // window.open). Staggered ~280ms so opening many GREENHOUSE tabs doesn't trip its burst
    // rate-limit (the 406). Arms them so the popup's "Fill ALL" fills each.
    (async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const ids = [];
      for (const url of (msg.urls || []).slice(0, 20)) {
        try { const t = await chrome.tabs.create({ url, active: false }); if (t && t.id) ids.push(t.id); }
        catch (e) {}
        await sleep(280);
      }
      if (msg.arm !== false && ids.length) {
        const a = await _getArmed();
        ids.forEach((id) => a.add(id));
        await _setArmed(a);
      }
      sendResponse({ ok: true, opened: ids.length });
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
        // Remember the job URL for this tab so resolve/resume/cover can still match the job after
        // "Apply Now" navigates to an apply page that drops the job id / referrer.
        if (tabId != null) {
          try {
            await chrome.storage.session.set({
              ["src_" + tabId]: msg.url, last_src: msg.url, last_src_ts: Date.now(),
            });
          } catch (e) {}
        }
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
    (async () => {
      const origin_url = await _srcFor(_sender);
      try {
        const r = await fetch(`${BASE}/resolve`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: msg.url || "", referrer: msg.referrer || "",
                                 origin_url, fields: msg.fields || [] }),
        });
        sendResponse({ ok: true, data: await r.json() });
      } catch (e) { sendResponse({ ok: false, error: String(e) }); }
    })();
    return true;
  }

  if (msg && msg.type === "domdump") {
    fetch(`${BASE}/domdump`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: msg.url || "", fields: msg.fields || [] }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse(data))
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
    (async () => {
      const origin = await _srcFor(_sender);
      const qs = new URLSearchParams({ url: msg.url || "", referrer: msg.referrer || "", origin });
      const fallbackName = msg.type === "cover" ? "cover_letter.pdf" : "resume.pdf";
      try {
        const r = await fetch(`${BASE}/${msg.type}?${qs.toString()}`);
        if (!r.ok) return sendResponse({ ok: false, status: r.status });
        const buf = await r.arrayBuffer();
        const cd = r.headers.get("content-disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        sendResponse({ ok: true, base64: bufToBase64(buf), filename: m ? m[1] : fallbackName });
      } catch (e) { sendResponse({ ok: false, error: String(e) }); }
    })();
    return true;
  }
});
