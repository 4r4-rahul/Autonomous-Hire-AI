// Runs on the AutonomousHire dashboard (localhost). Lets the dashboard's "Open all" open tabs via the
// EXTENSION (chrome.tabs.create) instead of window.open — the pop-up blocker doesn't apply to
// extension-opened tabs, so all of a group's roles open in one click.
//
// Handshake: we mark the page (shared DOM) so the dashboard knows the extension is present, then relay
// its "open_tabs" postMessage to the background worker.
try { document.documentElement.setAttribute("data-ah-ext", "1"); } catch (e) {}

window.addEventListener("message", (e) => {
  if (e.source !== window) return;                       // only same-page messages
  const d = e.data;
  if (!d || d.source !== "ah-dashboard" || d.type !== "open_tabs" || !Array.isArray(d.urls)) return;
  chrome.runtime.sendMessage(
    { type: "open_tabs", urls: d.urls.slice(0, 20), arm: d.arm !== false },
    (resp) => {
      // tell the dashboard how many actually opened (for its status line)
      window.postMessage({ source: "ah-ext", type: "opened", opened: (resp && resp.opened) || 0 }, "*");
    });
});
