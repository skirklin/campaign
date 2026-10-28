import { characterList, sheetView } from "./sheet.js";
import { campaignList, campaignView, entityView, sourceView } from "./campaigns.js";

const routes = [
  [/^#\/characters\/(.+)$/, (m) => sheetView(decodeURIComponent(m[1])), "characters"],
  [/^#\/campaigns\/([^/]+)\/source\/(\d+)$/, (m) => sourceView(m[1], m[2]), "campaigns"],
  [/^#\/campaigns\/([^/]+)\/entity\/(\d+)$/, (m) => entityView(m[1], m[2]), "campaigns"],
  [/^#\/campaigns\/([^/]+)(?:\/(\w+))?$/, (m) => campaignView(m[1], m[2] || "ingest"), "campaigns"],
  [/^#\/campaigns\/?$/, () => campaignList(), "campaigns"],
  [/^.*$/, () => characterList(), "characters"],
];

let current = 0;

async function route() {
  const hash = location.hash || "#/characters";
  const view = document.getElementById("view");
  const token = ++current;
  for (const [re, render, nav] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
    try {
      const el = await render(m);
      if (token === current) view.replaceChildren(el);
    } catch (err) {
      if (token === current) view.replaceChildren(Object.assign(document.createElement("p"), { className: "empty", textContent: err.message }));
    }
    return;
  }
}

window.addEventListener("hashchange", route);
route();

// Show who's signed in when sign-in is on (/auth/me is a 404 when it's off).
fetch("/auth/me").then((r) => (r.ok ? r.json() : null)).then((me) => {
  if (!me?.email) return;
  document.getElementById("account-email").textContent = me.email;
  document.getElementById("account").hidden = false;
}).catch(() => {});
