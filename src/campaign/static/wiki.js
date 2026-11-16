// The world as a wiki: names in running text link to their pages.
//
// Which words link where comes from the server (World.links), made for this reader: a player
// never gets a link to something the party doesn't know of, or by a name they don't know. Text
// is linked with the same rule the server uses to find where a page is mentioned: the longest
// name wins ("Temple of the Oracle" is the temple, not the Oracle), names match by case, and
// a page doesn't link to itself.

import { api, h } from "./util.js";

const indexes = new Map();

// Resolves to a campaign's linker for this reader: link(root, {self}) links the text under root.
// Fetched at most every 30 s, like the session names.
export async function nameLinks(slug) {
  let got = indexes.get(slug);
  if (!got || Date.now() - got.at > 30000) {
    got = { at: Date.now(), index: api("GET", `/api/campaigns/${slug}/links`).catch(() => ({ links: {}, entities: {} })) };
    indexes.set(slug, got);
  }
  return linker(slug, await got.index);
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Where text isn't linked: links and controls already, headings and labels, code and stat blocks.
const SKIP = "a, button, code, pre, h1, h2, h3, h4, summary, dt, label, select, option, textarea, input,"
  + " .badge, .statblock-card, .encounter-card, .no-links";

export function linker(slug, { links, entities }) {
  const names = Object.keys(links).sort((a, b) => b.length - a.length);
  const pattern = names.length
    ? new RegExp(`(?<![\\p{L}\\p{N}_])(?:${names.map(escape).join("|")})(?![\\p{L}\\p{N}_])`, "gu") : null;
  const href = (id) => `#/campaigns/${slug}/entity/${id}`;

  // Link every name in the text under `root` (a page about `self` leaves its own name as text).
  // Returns the ids it linked to.
  const link = (root, { self = null } = {}) => {
    const linked = new Set();
    if (!pattern || !root) return linked;
    const texts = [];
    const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement?.closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    while (walk.nextNode()) texts.push(walk.currentNode);
    for (const node of texts) {
      const text = node.nodeValue;
      const parts = [];
      let last = 0;
      for (const m of text.matchAll(pattern)) {
        const id = links[m[0]];
        if (id === undefined || String(id) === String(self)) continue;
        parts.push(text.slice(last, m.index),
          h("a.wiki", { href: href(id), title: `${entities[id]?.name || m[0]}`, dataset: { entity: id } }, m[0]));
        linked.add(id);
        last = m.index + m[0].length;
      }
      if (parts.length) node.replaceWith(...parts, text.slice(last));
    }
    return linked;
  };
  return { link, href, entities };
}
