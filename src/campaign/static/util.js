// Small DOM and fetch helpers shared by the views.

export async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 401 && url.startsWith("/api/")) {
    location.href = `/auth/login?next=${encodeURIComponent(location.pathname)}`;
    throw new Error("Signing in…");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : detail || res.statusText;
    throw new Error(msg);
  }
  return data;
}

// h("div.card", {onclick: f}, "text", child) -> element. Attributes starting
// with "on" become listeners; null/false children are skipped.
export function h(spec, attrs = {}, ...children) {
  if (attrs === null || typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs)) {
    children.unshift(attrs);
    attrs = {};
  }
  const [tag, ...classes] = spec.split(".");
  const el = document.createElement(tag || "div");
  if (classes.length) el.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false || k === "value") continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && k !== "list" && k !== "form") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  // After children, so a <select> already has the option being selected.
  if (attrs.value !== undefined && attrs.value !== null) el.value = attrs.value;
  return el;
}

let toastTimer;
// A message at the bottom of the screen. With `action` ({label, run}), it carries a button
// (Undo) and stays a little longer.
export function toast(msg, isError = false, action = null) {
  const el = document.getElementById("toast");
  el.replaceChildren(h("span", msg), ...(action ? [h("button.toast-action", {
    onclick: () => { el.className = ""; action.run(); },
  }, action.label)] : []));
  el.className = "show" + (isError ? " error" : "") + (action ? " has-action" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), isError ? 6000 : action ? 5000 : 2500);
}

export const sign = (n) => (n >= 0 ? `+${n}` : `${n}`);
export const enc = encodeURIComponent;

export function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

export function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const target = keys.reduce((o, k) => o[k], obj);
  target[last] = value;
}

export function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// Who's signed in: {local: true} without sign-in, else {id, email, is_admin}. Fetched once.
let mePromise;
export function whoami() {
  return (mePromise ||= api("GET", "/api/me"));
}

// Viewers can look but not change; editors and owners can change content.
export const canEdit = (campaign) => campaign.access !== "viewer";

// Live updates for a campaign over Server-Sent Events: handlers[type](data)
// for each event. The browser reconnects by itself (after a deploy, say), and
// the stream closes once `owner` has been on the page and left it.
export function live(slug, owner, handlers) {
  const es = new EventSource(`/api/campaigns/${slug}/events`);
  let seen = false;
  const gone = () => {
    if (owner.isConnected) seen = true;
    if (seen && !owner.isConnected) {
      es.close();
      clearInterval(timer);
      return true;
    }
    return false;
  };
  const timer = setInterval(gone, 2000);
  for (const [type, fn] of Object.entries(handlers)) {
    es.addEventListener(type, (e) => {
      if (!gone()) fn(JSON.parse(e.data));
    });
  }
  return () => { es.close(); clearInterval(timer); };
}

// POST JSON and read the response as Server-Sent Events: onEvent(type, data) for each.
export async function streamPost(url, body, onEvent) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = (await res.json().catch(() => null))?.detail;
    throw new Error(typeof detail === "string" ? detail : res.statusText);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let type = "message", data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(type, JSON.parse(data));
    }
  }
}

// Let the browser's back (a swipe on a phone) close an overlay rather than leave the page.
// Opening one adds a history entry; going back calls onBack. Closing it any other way
// calls the returned function, which takes that entry off again. Entries are added and
// removed one at a time, so an overlay opened as another closes can't lose its own.
let settled = Promise.resolve();
export function backCloses(onBack) {
  let pushed = false, done = false;
  const pop = () => {
    window.removeEventListener("popstate", pop);
    pushed = false;
    if (!done) { done = true; onBack(); }
  };
  settled.then(() => {
    if (done) return;
    history.pushState({ overlay: true }, "");
    pushed = true;
    window.addEventListener("popstate", pop);
  });
  return () => {
    if (done) return;
    done = true;
    window.removeEventListener("popstate", pop);
    if (!pushed) return;
    settled = new Promise((resolve) => {
      const back = () => { window.removeEventListener("popstate", back); resolve(); };
      window.addEventListener("popstate", back);
      history.back();
    });
  };
}

// A sheet that slides up from the bottom of the screen; closes on ×, a tap outside, or back.
export function bottomSheet(title, ...body) {
  const dialog = h("dialog.bottom-sheet",
    h("div.sheet-grip"),
    h("div.sheet-title", h("h2", title), h("button.icon", { onclick: () => dialog.close(), "aria-label": "Close" }, "×")),
    ...body);
  const release = backCloses(() => dialog.close());
  dialog.addEventListener("close", () => { release(); dialog.remove(); });
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

// A number pad, so the phone's keyboard doesn't cover the sheet. onAmount(n) on each press.
export function keypad(onAmount) {
  let amount = "";
  const display = h("div.keypad-display", "0");
  const press = (k) => {
    amount = k === "⌫" ? amount.slice(0, -1) : k === "C" ? "" : (amount + k).replace(/^0+/, "").slice(0, 5);
    display.textContent = amount || "0";
    onAmount(Number(amount) || 0);
  };
  return [display, h("div.keypad", ["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "⌫"].map((k) =>
    h("button", { onclick: () => press(k) }, k)))];
}

// How many of something, 1..max: resolves to the number, or null if dismissed.
export function askQty(title, max, verb) {
  if (max <= 1) return Promise.resolve(1);
  return new Promise((resolve) => {
    let n = 1, done = null;
    const out = h("span.step-value", n);
    const step = (by) => { n = Math.max(1, Math.min(max, n + by)); out.textContent = n; };
    const dialog = bottomSheet(title,
      h("div.qty-pick",
        h("span.stepper", h("button", { onclick: () => step(-1), "aria-label": "One less" }, "−"), out,
          h("button", { onclick: () => step(1), "aria-label": "One more" }, "+")),
        h("button", { onclick: () => { n = max; out.textContent = n; } }, `All ${max}`)),
      h("button.primary.wide", { onclick: () => { done = n; dialog.close(); } }, verb));
    dialog.addEventListener("close", () => resolve(done));
  });
}

// -- installing the app ------------------------------------------------------------
// When the browser says the app can be installed (Chrome on Android), offer it in the
// page instead of Chrome's own bar. Installed, it opens in its own window, without the
// browser's address and tab bars.
let installEvent = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installEvent = e;
  document.dispatchEvent(new Event("installable"));
});
window.addEventListener("appinstalled", () => {
  installEvent = null;
  document.dispatchEvent(new Event("installable"));
});

export function installBanner() {
  const dismissed = () => { try { return localStorage.getItem("install-dismissed") === "1"; } catch { return false; } };
  const el = h("div.install-banner", { hidden: true },
    h("span", h("strong", "Install Campaign as an app"), h("span.muted.small", " It opens full screen, without the browser's bars.")),
    h("button.primary", {
      onclick: async () => {
        if (!installEvent) return;
        installEvent.prompt();
        await installEvent.userChoice;
        installEvent = null;
        el.hidden = true;
      },
    }, "Install"),
    h("button.icon", {
      "aria-label": "Not now", title: "Not now",
      onclick: () => { try { localStorage.setItem("install-dismissed", "1"); } catch { /* not remembered */ } el.hidden = true; },
    }, "×"));
  const update = () => { el.hidden = !installEvent || dismissed(); };
  document.addEventListener("installable", update);
  update();
  return el;
}
