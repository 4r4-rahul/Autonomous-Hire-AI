const statusEl = document.getElementById("status");

document.getElementById("fill").addEventListener("click", async () => {
  statusEl.textContent = "Filling…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id || /^(chrome|edge|about|chrome-extension):/.test(tab.url || "")) {
      statusEl.textContent = "Open a job application page first.";
      return;
    }
    // allFrames:true so embedded forms (e.g. Greenhouse iframe on a company careers
    // page) get filled too — the form often isn't in the top frame.
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["content.js"],
    });
    statusEl.textContent = `Ran in ${results.length} frame(s) — check the page panel.`;
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});

// Hosts that are job-application forms — so "Fill ALL" injects into your application tabs and
// not random tabs (gmail, docs, etc.).
const _ATS_HOST = /(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|icims\.com|myworkdayjobs\.com|jobvite\.com|breezy\.hr|bamboohr\.com|teksystems\.com|kforce\.com|insightglobal|jobs\.|careers\.|\/jobs\/|\/careers\/|\/application|\/apply)/i;

document.getElementById("fillAll").addEventListener("click", async () => {
  statusEl.textContent = "Finding application tabs…";
  try {
    const tabs = await chrome.tabs.query({});   // every window
    const withUrl = tabs.filter((t) => t.id && t.url);
    const targets = withUrl.filter((t) => /^https?:/.test(t.url) && _ATS_HOST.test(t.url));
    console.log("[AH] fillAll — tabs:", tabs.length, "readable URLs:", withUrl.length,
                "matched:", targets.length, targets.map((t) => t.url));
    if (!withUrl.length) {
      // tabs.query returned no URLs → 'tabs' permission not granted yet (extension needs a full reload).
      statusEl.textContent = "Can't read tab URLs — REMOVE & re-add the extension (new 'tabs' permission).";
      return;
    }
    if (!targets.length) {
      statusEl.textContent = `No application tabs matched (${tabs.length} open). Open apply tabs first.`;
      return;
    }
    // Inject the filler into EACH application tab. Each tab's content script runs independently
    // (its own /resolve call) → concurrent fills. Filling never submits. Chrome throttles
    // BACKGROUND tabs, so the focused one fills full-speed and the rest finish as you switch.
    let ok = 0; const errs = [];
    for (const t of targets) {
      try {
        await chrome.scripting.executeScript({ target: { tabId: t.id, allFrames: true }, files: ["content.js"] });
        ok++;
      } catch (e) { errs.push(String((e && e.message) || e).slice(0, 50)); }
    }
    statusEl.textContent = ok
      ? `Filling ${ok}/${targets.length} tabs — switch through to review + submit.`
      : `Matched ${targets.length} but inject failed: ${errs[0] || "?"} — reload the extension.`;
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});
