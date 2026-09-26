// Finishing a captcha the app handed to the browser (engine/challenge_handoff.py).
//
// The app opens the challenge page with a ticket in the URL fragment, which
// the site never receives. This runs before the page's own scripts: it takes
// the ticket and removes it from the address bar, then waits for the
// captcha widget's answer and hands both to the background worker, which adds
// this origin's cookies and returns them to the app over native messaging.
// Nothing happens on pages the app did not open.

const FRAGMENT = "mossdl-handoff";
const ANSWER_FIELDS = ["cf-turnstile-response", "g-recaptcha-response", "h-captcha-response"];

function takeTicket(): { ticket: string; challengeId: string; generation: number } | null {
  const params = new URLSearchParams(location.hash.slice(1));
  const ticket = params.get(FRAGMENT);
  const challengeId = params.get("mossdl-c");
  const generation = Number(params.get("mossdl-g"));
  if (!ticket || !challengeId || !Number.isInteger(generation)) return null;
  // Out of the address bar and history before the site can read it.
  history.replaceState(history.state, "", location.pathname + location.search);
  return { ticket, challengeId, generation };
}

function answer(): string | null {
  for (const name of ANSWER_FIELDS) {
    for (const field of Array.from(document.getElementsByName(name))) {
      const value = (field as HTMLInputElement | HTMLTextAreaElement).value;
      if (value && value.length > 20) return value;
    }
  }
  return null;
}

const handoff = window.top === window ? takeTicket() : null;
if (handoff) {
  const runtime = (globalThis as any).browser?.runtime ?? (globalThis as any).chrome?.runtime;
  let sent = false;
  const send = () => {
    const token = answer();
    if (sent || !token) return;
    sent = true;
    observer.disconnect();
    runtime?.sendMessage?.({ type: "challenge_solved", ...handoff, token });
  };
  const observer = new MutationObserver(send);
  const start = () => {
    observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ["value"] });
    // Widgets set .value without an attribute change; a light poll covers that.
    const poll = window.setInterval(() => { send(); if (sent) window.clearInterval(poll); }, 1000);
    window.setTimeout(() => window.clearInterval(poll), 15 * 60 * 1000); // the ticket's own lifetime
  };
  if (document.documentElement) start();
  else document.addEventListener("DOMContentLoaded", start, { once: true });
}
