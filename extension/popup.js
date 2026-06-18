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
    // Chrome freezes timers in BACKGROUND tabs, so we can't fill all 10 at once. Instead: fill the
    // tab you're looking at NOW, and ARM the rest — the background worker fills each the instant you
    // switch to it (full speed). Zero extra clicks; you just tab through to review + submit.
    const active = targets.find((t) => t.active)
      || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
    let filledActive = false;
    if (active && active.id && _ATS_HOST.test(active.url || "")) {
      try {
        await chrome.scripting.executeScript({ target: { tabId: active.id, allFrames: true }, files: ["content.js"] });
        filledActive = true;
      } catch (e) { /* fall through */ }
    }
    const rest = targets.filter((t) => !active || t.id !== active.id);
    await chrome.runtime.sendMessage({ type: "arm", tabIds: rest.map((t) => t.id) });
    statusEl.textContent = (filledActive ? "Filling this tab; " : "")
      + `${rest.length} more armed — each fills the moment you switch to it. Review + submit per tab.`;
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});
