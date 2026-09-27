// Watch a claude.ai tab and report when Claude starts and stops replying.
// The background worker adds the tab id and forwards it to bin/yogo-web.

// ---------------------------------------------------------------------------
// SELECTORS: what "Claude is generating" looks like in the page. claude.ai
// changes its markup from time to time; when the display stops reacting,
// fix these first. Either one matching counts as generating.
const SELECTORS = {
  // the message that is still streaming in
  streaming: '[data-is-streaming="true"]',
  // the Stop button shown while a reply is in progress (case-insensitive match)
  stopButton: "button[aria-label]",
  stopLabel: /stop/i,
};
// ---------------------------------------------------------------------------

const POLL_MS = 500;
const HEARTBEAT_MS = 60_000; // under the listener's 120 s thinking TTL

function visible(el) {
  return el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
}

function generating() {
  if (document.querySelector(SELECTORS.streaming)) return true;
  for (const b of document.querySelectorAll(SELECTORS.stopButton)) {
    if (SELECTORS.stopLabel.test(b.getAttribute("aria-label")) && visible(b)) return true;
  }
  return false;
}

function send(state) {
  try {
    chrome.runtime.sendMessage({ state }).catch(() => {});
  } catch {
    // extension was reloaded; this old content script can no longer talk to it
    clearInterval(timer);
  }
}

let busy = false;
let lastSent = 0;

const timer = setInterval(() => {
  const now = generating();
  if (now && (!busy || Date.now() - lastSent >= HEARTBEAT_MS)) {
    send("thinking");
    lastSent = Date.now();
  } else if (!now && busy) {
    send("done");
  }
  busy = now;
}, POLL_MS);
