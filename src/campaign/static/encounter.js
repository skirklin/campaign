// The encounter card: a ```encounter block from the assistant ({"creatures": [{name, count, hp, note}],
// "party": {size, level}}) rendered as the fight it describes. The block is only the spec; the
// stat blocks and the difficulty come from the server, so the answer stays short and the card
// shows the world as it is now.

import { api, h, toast } from "./util.js";
import { parseExpr, rollExpr } from "./dice.js";
import { statBlockFromData } from "./markdown.js";

const built = new Map();  // spec text -> Promise of the server's result, so a re-render doesn't refetch

export function encounterFence(slug) {
  return (body) => {
    let spec;
    try {
      spec = JSON.parse(body);
    } catch {
      return h("div.encounter-card.pending", "Putting the encounter together…");  // still streaming in
    }
    const card = h("section.encounter-card", h("p.muted.small", "Putting the encounter together…"));
    const key = `${slug}\n${JSON.stringify(spec)}`;
    if (!built.has(key)) built.set(key, api("POST", `/api/campaigns/${slug}/encounter`, spec));
    built.get(key).then(
      (result) => card.replaceChildren(...encounterBody(result, slug)),
      (err) => card.replaceChildren(h("p.error-text", `Couldn't build this encounter: ${err.message}`)),
    );
    return card;
  };
}

const mod = (score) => (Number.isFinite(score) ? Math.floor((score - 10) / 2) : 0);
const averageHp = (hp) => Number(String(hp || "").match(/^\s*(\d+)/)?.[1]) || null;

// The book text a stat block was read from, fetched when opened: to check the card against.
function bookText(slug, entityId) {
  const body = h("div.book-text", h("p.muted.small", "Finding it in the book…"));
  let loaded = false;
  return h("details.book-text-toggle", {
    ontoggle: async (e) => {
      if (!e.target.open || loaded) return;
      loaded = true;
      try {
        const chunks = await api("GET", `/api/campaigns/${slug}/entities/${entityId}/passage`);
        body.replaceChildren(...(chunks.length
          ? chunks.map((ch) => h("div", h("p.fact-meta", [ch.source, ch.heading, ch.page && `p. ${ch.page}`].filter(Boolean).join(" · ")),
            h("pre", ch.text)))
          : [h("p.muted.small", "Couldn't find this stat block's text in the book.")]));
      } catch (err) {
        body.replaceChildren(h("p.error-text", err.message));
      }
    },
  }, h("summary.small", "Book text"), body);
}

function encounterBody(r, slug) {
  const party = r.party ? `${r.party.size} characters, level ${r.party.level}` : "no party on file";
  const head = h("header",
    h("h4", "Encounter"),
    r.difficulty && h(`span.difficulty.${r.difficulty}`, r.difficulty),
    h("span.muted.small", [
      `${r.monsters} monster${r.monsters === 1 ? "" : "s"} · ${r.xp.toLocaleString()} XP`,
      r.adjusted_xp != null ? ` (${r.adjusted_xp.toLocaleString()} adjusted, ×${r.multiplier})` : "",
      ` · ${party}`,
      r.xp_complete ? "" : " · some XP unknown",
    ].join("")));
  const thresholds = r.thresholds && h("p.muted.small.thresholds",
    Object.entries(r.thresholds).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(" · "));

  const initiative = h("ol.initiative");
  const roll = h("button", {
    type: "button",
    onclick: () => {
      const rolls = r.creatures.filter((c) => c.found).map((c) => {
        const m = mod(c.stat_block.dexterity);
        const res = rollExpr(parseExpr(`1d20${m >= 0 ? "+" : ""}${m}`));
        return { name: c.count > 1 ? `${c.name} ×${c.count}` : c.name, total: res.total, detail: res.detail };
      }).sort((a, b) => b.total - a.total);
      initiative.replaceChildren(...rolls.map((x) => h("li", h("strong", x.total), ` ${x.name}`, h("span.muted.small", ` (${x.detail})`))));
      toast("Initiative rolled for the monsters");
    },
  }, "Roll initiative");

  const rows = r.creatures.map((c) => {
    const sb = c.stat_block;
    if (!sb) {
      return h("div.encounter-creature.missing",
        h("strong", `${c.count} × ${c.asked_for}`),
        h("p.small", "No stat block for this in your material. Look it up in its own book."));
    }
    const hp = averageHp(c.hp);
    const trackers = hp && h("div.hp-trackers", Array.from({ length: c.count }, (_, i) => h("label.hp-tracker",
      h("span.small", c.count > 1 ? `#${i + 1}` : "HP"),
      h("input", { type: "number", value: hp, min: 0, max: hp * 2, "aria-label": `${c.name} ${i + 1} hit points` }))));
    return h("details.encounter-creature", { open: r.creatures.length === 1 },
      h("summary",
        h("strong", `${c.count} × ${c.name}`),
        h("span.small.muted", [
          `AC ${sb.armor_class}`, `HP ${c.hp}${c.hp_changed ? " (adventure)" : ""}`,
          c.cr && `CR ${c.cr}`, c.xp_each != null && `${c.xp_each.toLocaleString()} XP each`,
        ].filter(Boolean).join(" · ")),
        c.note && h("span.small.encounter-note", c.note)),
      trackers,
      statBlockFromData(c.name, { ...sb, hit_points: c.hp }),
      c.id && bookText(slug, c.id),
    );
  });
  return [head, thresholds, h("div.encounter-actions", roll), initiative, ...rows];
}
