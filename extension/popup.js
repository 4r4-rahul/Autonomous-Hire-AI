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
    const targets = tabs.filter(
      (t) => t.id && t.url && /^https?:/.test(t.url) && _ATS_HOST.test(t.url));
    if (!targets.length) {
      statusEl.textContent = "No application tabs open. Open some first (script: --open N).";
      return;
    }
    // Kick off the fill in EACH tab. Each tab's content script runs independently (its own
    // /resolve call), so they fill concurrently. Filling never submits — you still review +
    // CAPTCHA + submit per tab. NOTE: Chrome throttles BACKGROUND tabs, so the tab you're
    // looking at fills at full speed and the rest finish as you switch to them.
    let ok = 0;
    for (const t of targets) {
      try {
        await chrome.scripting.executeScript({
          target: { tabId: t.id, allFrames: true },
          files: ["content.js"],
        });
        ok++;
      } catch (e) { /* tab may be discarded / restricted — skip */ }
    }
    statusEl.textContent = `Started fill in ${ok}/${targets.length} tabs. Switch through them to ` +
      `review + submit (background tabs finish as you focus them).`;
  } catch (e) {
    statusEl.textContent = "Error: " + String(e && e.message ? e.message : e).slice(0, 120);
  }
});
