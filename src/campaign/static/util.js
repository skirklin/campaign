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
export function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), isError ? 6000 : 2500);
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
