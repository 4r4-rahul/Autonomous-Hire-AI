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
    // Without the 'tabs' permission GRANTED, query returns background tabs with url=undefined — only
    // the active tab is readable (via activeTab). So if many tabs are open but we can read ≤1 URL,
    // the permission wasn't granted (a reload doesn't grant it — the extension must be RE-ADDED).
    if (!withUrl.length || (withUrl.length <= 1 && tabs.length > 2)) {
      statusEl.textContent = `Can't see other tabs (read ${withUrl.length}/${tabs.length} URLs). ` +
        `REMOVE & re-add the extension to grant the 'tabs' permission.`;
      return;
    }
    if (!targets.length) {
      statusEl.textContent = `No application tabs matched (${tabs.length} open). Open apply tabs first.`;
      return;
    }
    // Inject the filler DIRECTLY into every application tab now. Each tab's content script runs
    // independently; Chrome throttles BACKGROUND tabs so they fill slowly, but they resume at full
    // speed the moment you focus them — so as you switch through, each is filled/finishing. (Direct
    // inject is simpler + more reliable than the old arm-on-focus path, which depended on the
    // service worker + storage all firing.) content.js self-guards against double-running.
    let ok = 0; const errs = [];
    for (const t of targets) {
      try {
        await chrome.scripting.executeScript({ target: { tabId: t.id, allFrames: true }, files: ["content.js"] });
        ok++;
      } catch (e) { errs.push(String((e && e.message) || e).slice(0, 50)); }
    }
    // Also arm them, so a tab Chrome DISCARDS (unloads) gets re-filled when you return to it.
    await chrome.runtime.sendMessage({ type: "arm", tabIds: targets.map((t) => t.id) }).catch(() => {});
    const diag = `[${tabs.length} open · ${withUrl.length} readable · ${targets.length} matched · ${ok} injected]`;
    statusEl.textContent = ok
      ? `Filling ${ok}/${targets.length} app tabs — switch through to review. ${diag}` +
        (errs.length ? ` ${errs.length} failed: ${errs[0]}` : "")
      : `Couldn't inject any tab. ${diag} ${errs[0] ? "err: " + errs[0] : ""}`;
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});
