import { characterList, sheetView } from "./sheet.js";
import { campaignList, campaignView } from "./campaigns.js";

const routes = [
  [/^#\/characters\/(.+)$/, (m) => sheetView(decodeURIComponent(m[1])), "characters"],
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
