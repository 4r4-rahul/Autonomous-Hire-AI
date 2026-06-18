const statusEl = document.getElementById("status");
// Bump this on every extension code change. If the popup doesn't show this exact build, the reload
// didn't take — remove & re-add the extension.
const BUILD = "capture-2 (2026-06-18)";
statusEl.textContent = "build " + BUILD;

// DEBUG: capture the current form's field structure (no personal values) so a bespoke ATS form
// (e.g. apply.teksystems.com custom widgets) can be reverse-engineered.
document.getElementById("capture").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id || /^(chrome|edge|about|chrome-extension):/.test(tab.url || "")) {
      statusEl.textContent = "Open the application form first, then capture.";
      return;
    }
    statusEl.textContent = "Capturing form DOM…";
    await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["capture.js"] });
    statusEl.textContent = "Capture sent — analysis next (see the green note on the page).";
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});

// PerimeterX sites (e.g. TEKsystems) 403 a normal tab but allow a fresh incognito session. This
// reopens the current job URL in an incognito window and auto-fills it there.
document.getElementById("incognito").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id || !/^https?:/.test(tab.url || "")) {
      statusEl.textContent = "Open the job page first (then reopen it in incognito).";
      return;
    }
    statusEl.textContent = "Opening in incognito…";
    const r = await chrome.runtime.sendMessage({ type: "open_incognito", url: tab.url, closeTabId: tab.id });
    statusEl.textContent = (r && r.ok)
      ? "Opened in incognito — it auto-fills there. Solve any Press-and-Hold, then submit."
      : "Couldn't open incognito: " + ((r && r.error) || "?") +
        " — enable 'Allow in incognito' at chrome://extensions → AutonomousHire → Details.";
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});

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
    // Inject the filler into every application tab IN PARALLEL. CRITICAL: do NOT await each tab
    // sequentially — chrome.scripting.executeScript blocks until the injected content script's async
    // work finishes (the résumé tailoring poll alone is up to ~50s), so a sequential loop serializes
    // the tabs and looks frozen ("no activity on other tabs" for minutes). Firing them concurrently
    // means every tab gets content.js immediately and starts filling at once. A per-tab timeout keeps
    // the popup status from waiting on a slow fill — injection already happened by then.
    statusEl.textContent = `Injecting ${targets.length} application tabs…`;
    const inject = (t) => Promise.race([
      chrome.scripting.executeScript({ target: { tabId: t.id, allFrames: true }, files: ["content.js"] })
        .then(() => "ok"),
      new Promise((res) => setTimeout(() => res("running"), 6000)),   // injected; still filling — fine
    ]).catch((e) => "ERR:" + String((e && e.message) || e).slice(0, 40));
    const results = await Promise.all(targets.map(inject));
    const ok = results.filter((r) => r === "ok" || r === "running").length;
    const errs = results.filter((r) => typeof r === "string" && r.startsWith("ERR:"));
    // Also arm them, so a tab Chrome DISCARDS (unloads) gets re-filled when you return to it.
    await chrome.runtime.sendMessage({ type: "arm", tabIds: targets.map((t) => t.id) }).catch(() => {});
    const diag = `[${tabs.length} open · ${withUrl.length} readable · ${targets.length} matched · ${ok} injected]`;
    statusEl.textContent = ok
      ? `Filling ${ok}/${targets.length} app tabs — switch through; each shows a blue badge + fills. ${diag}` +
        (errs.length ? ` ${errs.length} failed: ${errs[0]}` : "")
      : `Couldn't inject any tab. ${diag} ${errs[0] ? errs[0] : ""}`;
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});
