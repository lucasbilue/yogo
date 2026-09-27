// Forward claude.ai tab activity to the local yogo listener (bin/yogo-web).

const LISTENER = "http://127.0.0.1:7437/signal";

let warned = false;

function post(tab, state) {
  fetch(LISTENER, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Yogo": "1" },
    body: JSON.stringify({ tab, state }),
  }).catch((e) => {
    // listener not running: do nothing, but say so once
    if (!warned) {
      console.log(`yogo: can't reach ${LISTENER} (is ./bin/yogo-web running?)`, e);
      warned = true;
    }
  });
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (sender.tab && typeof msg?.state === "string") post(sender.tab.id, msg.state);
});

chrome.tabs.onRemoved.addListener((tabId) => post(tabId, "clear"));
