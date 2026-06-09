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
