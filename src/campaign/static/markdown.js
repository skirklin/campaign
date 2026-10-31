// Markdown from the assistant, rendered as DOM nodes (never innerHTML, so nothing
// in an answer can run as code). Covers what answers use: headings, paragraphs,
// lists, quotes (the book's read-aloud text), tables, code, rules, emphasis and
// links. Two kinds of game text get more than formatting:
//   - a ```statblock fence (the stat_block tool's text) becomes a stat block card;
//   - dice ("3d8 + 5", "+8 to hit") become chips that roll when tapped.

import { h, toast } from "./util.js";
import { parseExpr, rollExpr, formatExpr } from "./dice.js";

// `fences` renders other fenced blocks by language: {encounter: (body) => element}.
let fences = {};

export function renderMarkdown(text, custom = {}) {
  const outer = fences;
  fences = custom;
  try {
    return h("div.md", blocks(String(text || "").replace(/\r\n?/g, "\n").split("\n")));
  } finally {
    fences = outer;
  }
}

// -- blocks --------------------------------------------------------------------------

const FENCE = /^\s*(```+|~~~+)\s*([\w-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?/;
const ITEM = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function blocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    let m = line.match(FENCE);
    if (m) {
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith(m[1]); i++) body.push(lines[i]);
      i++;  // the closing fence (or the end, while an answer is still streaming in)
      const lang = m[2].toLowerCase();
      out.push(lang === "statblock" ? statBlockFromText(body.join("\n"))
        : fences[lang] ? fences[lang](body.join("\n"))
        : h("pre", h("code", body.join("\n"))));
      continue;
    }
    if ((m = line.match(HEADING))) {
      // An answer's own headings sit below the page's: # is a section, not a page title.
      out.push(h(`h${Math.min(m[1].length + 2, 6)}`, inline(m[2])));
      i++;
      continue;
    }
    if (RULE.test(line)) { out.push(h("hr")); i++; continue; }
    if (QUOTE.test(line)) {
      const body = [];
      for (; i < lines.length && lines[i].trim() && QUOTE.test(lines[i]); i++) body.push(lines[i].replace(QUOTE, ""));
      out.push(h("blockquote", blocks(body)));
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const rows = [line];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(lines[i]);
      out.push(table(rows));
      continue;
    }
    if (ITEM.test(line)) {
      const start = i;
      const indent = line.match(ITEM)[1].length;
      for (i++; i < lines.length; i++) {
        const l = lines[i];
        if (!l.trim()) {
          // A blank line ends the list unless more of it follows, indented or as another item.
          const next = lines[i + 1];
          if (next === undefined || !(next.match(ITEM)?.[1].length >= indent || /^\s{2,}\S/.test(next))) break;
          continue;
        }
        const item = l.match(ITEM);
        if (item && item[1].length < indent) break;
        if (!item && !/^\s/.test(l) && (HEADING.test(l) || QUOTE.test(l) || FENCE.test(l) || RULE.test(l))) break;
      }
      out.push(list(lines.slice(start, i), indent));
      continue;
    }
    const para = [];
    for (; i < lines.length && lines[i].trim(); i++) {
      const l = lines[i];
      if (para.length && (HEADING.test(l) || FENCE.test(l) || QUOTE.test(l) || RULE.test(l) || ITEM.test(l))) break;
      para.push(l.trim());
    }
    out.push(h("p", para.flatMap((l, n) => (n ? [h("br"), ...inline(l)] : inline(l)))));
  }
  return out;
}

function list(lines, indent) {
  const ordered = /\d/.test(lines[0].match(ITEM)[2]);
  const items = [];
  for (const l of lines) {
    const m = l.match(ITEM);
    if (m && m[1].length <= indent + 1) items.push([m[3]]);
    else items[items.length - 1]?.push(l.slice(Math.min(indent + 2, l.length - l.trimStart().length)));
  }
  const first = Number(lines[0].match(ITEM)[2].replace(/\D/g, "")) || 1;
  return h(ordered ? "ol" : "ul", ordered && first !== 1 ? { start: first } : {},
    items.map((body) => {
      // A one-line item stays inline; anything more (a nested list, a second paragraph) is blocks.
      const tight = body.length === 1 || body.slice(1).every((l) => !l.trim());
      return h("li", tight ? inline(body[0]) : blocks(body));
    }));
}

function table(rows) {
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const [head, ...body] = rows.map(cells);
  return h("div.table-wrap", h("table",
    h("thead", h("tr", head.map((c) => h("th", inline(c))))),
    h("tbody", body.map((r) => h("tr", head.map((_, k) => h("td", inline(r[k] ?? "")))))),
  ));
}

// -- inline --------------------------------------------------------------------------

const INLINE = [
  ["code", /`([^`]+)`/],
  ["link", /\[([^\]]+)\]\(([^)\s]+)\)/],
  ["auto", /\bhttps?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/],
  ["strong", /\*\*(?=\S)([\s\S]*?\S)\*\*|__(?=\S)([\s\S]*?\S)__/],
  ["del", /~~(?=\S)([\s\S]*?\S)~~/],
  ["em", /\*(?=[^\s*])([^*]*?[^\s*])\*|(?<![\w])_(?=\S)([^_]*?\S)_(?![\w])/],
];

export function inline(text) {
  const out = [];
  let rest = String(text);
  while (rest) {
    let best = null;
    for (const [kind, re] of INLINE) {
      const m = rest.match(re);
      if (m && (best === null || m.index < best.m.index)) best = { kind, m };
    }
    if (!best) { out.push(...dice(rest)); break; }
    const { kind, m } = best;
    if (m.index) out.push(...dice(rest.slice(0, m.index)));
    const inner = m[1] ?? m[2] ?? "";
    if (kind === "code") out.push(h("code", inner));
    else if (kind === "link") out.push(link(m[2], inline(m[1])));
    else if (kind === "auto") out.push(link(m[0], m[0]));
    else out.push(h(kind, inline(inner)));
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

function link(href, label) {
  // Only web links and the app's own pages; anything else ("javascript:") stays text.
  if (/^#\//.test(href)) return h("a", { href }, label);
  if (/^https?:\/\//i.test(href)) return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, label);
  return h("span", label);
}

// -- dice --------------------------------------------------------------------------

// "3d8 + 5", "2d6", "1d20+8", and attack bonuses written "+8 to hit".
const DICE = /(\b\d{0,3}d\d{1,3}(?:\s*[+−-]\s*\d{1,3}(?!\s*d\d))?\b)|([+−-]\d{1,2})(?=\s+to hit\b)/gi;

function dice(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(DICE)) {
    const expr = m[1] ? m[1].replace(/−/g, "-") : `1d20${m[2].replace(/−/g, "-")}`;
    const parsed = parseExpr(expr);
    if (!parsed || !parsed.dice.length) continue;
    out.push(text.slice(last, m.index), diceChip(m[0], parsed));
    last = m.index + m[0].length;
  }
  out.push(text.slice(last));
  return out.filter((x) => x !== "");
}

export function diceChip(label, parsed) {
  const chip = h("button.dice-chip", {
    type: "button",
    title: `Roll ${formatExpr(parsed)}`,
    onclick: () => {
      const r = rollExpr(parsed);
      chip.dataset.result = r.total;
      chip.classList.remove("rolled");
      void chip.offsetWidth;  // restart the animation on a second roll
      chip.classList.add("rolled");
      const nat = r.natural === 20 ? " — natural 20!" : r.natural === 1 ? " — natural 1" : "";
      toast(`${formatExpr(parsed)} → ${r.total}  (${r.detail})${nat}`);
    },
  }, label);
  return chip;
}

// -- stat blocks --------------------------------------------------------------------

const ABILITIES = ["str", "dex", "con", "int", "wis", "cha"];
const ABILITY_NAMES = { strength: "str", dexterity: "dex", constitution: "con", intelligence: "int", wisdom: "wis", charisma: "cha" };
const modOf = (score) => (Number.isFinite(score) ? Math.floor((score - 10) / 2) : null);

// The stat_block tool's text: name, "AC … · HP … · Speed … · CR …", ability line, other
// lines, then "Traits:", "Actions:" (or other) sections of "- " items. Anything that
// doesn't look like that is shown as it is.
export function statBlockFromText(text) {
  const lines = text.split("\n").map((l) => l.trim());
  const name = lines.shift() || "";
  const top = lines.shift() || "";
  const parts = Object.fromEntries(top.split(/\s+·\s+/).map((p) => {
    const m = p.match(/^(AC|HP|Speed|CR)\s+(.*)$/i);
    return m ? [m[1].toLowerCase(), m[2]] : [p, ""];
  }));
  if (!parts.ac || !parts.hp) return h("pre", h("code", text));
  const scores = {};
  const props = [];
  const sections = [];
  for (const l of lines) {
    if (!l) continue;
    const abil = [...l.matchAll(/\b(STR|DEX|CON|INT|WIS|CHA)\s+(\d+|—|-)/g)];
    if (abil.length >= 3) {
      abil.forEach(([, k, v]) => (scores[k.toLowerCase()] = /\d/.test(v) ? Number(v) : null));
      continue;
    }
    const head = l.match(/^([A-Z][\w ]{1,30}):$/);
    if (head) { sections.push([head[1], []]); continue; }
    const item = l.replace(/^[-*•]\s+/, "");
    if (sections.length && item !== l) sections[sections.length - 1][1].push(item);
    else if (sections.length) sections[sections.length - 1][1].push(l);
    else props.push(l);
  }
  return statBlockCard({ name, ac: parts.ac, hp: parts.hp, speed: parts.speed, cr: parts.cr, scores, props, sections });
}

// The digest's structured stat block (as stored on the world's entities).
export function statBlockFromData(name, sb) {
  const scores = {};
  for (const [full, k] of Object.entries(ABILITY_NAMES)) scores[k] = sb[full] ?? null;
  return statBlockCard({
    name, ac: sb.armor_class, hp: sb.hit_points, speed: sb.speed, cr: sb.challenge, scores,
    props: sb.other || [],
    sections: [["Traits", sb.traits || []], ["Actions", sb.actions || []]],
  });
}

function statBlockCard({ name, ac, hp, speed, cr, scores, props, sections }) {
  const entry = (text) => {
    // "Tusk. Melee Weapon Attack: …" -> the name in bold italics, as the books print it.
    const m = text.match(/^([^.:]{2,60}?(?:\([^)]*\))?)\.\s+(.*)$/s);
    return h("p", m ? [h("strong", h("em", `${m[1]}.`)), " ", ...inline(m[2])] : inline(text));
  };
  const hpDice = String(hp || "").match(/\(([^)]*d[^)]*)\)/);
  return h("section.statblock-card",
    h("header",
      h("h4", name),
      cr && h("span.sb-cr", `CR ${cr}`)),
    h("div.sb-vitals",
      h("div", h("span.label", "Armor Class"), h("span", ac)),
      h("div", h("span.label", "Hit Points"), h("span", hpDice ? [hp.replace(hpDice[0], "").trim(), " ", ...dice(hpDice[0])] : hp)),
      speed && h("div", h("span.label", "Speed"), h("span", speed))),
    Object.values(scores).some((v) => v !== null && v !== undefined) && h("div.sb-abilities",
      ABILITIES.map((k) => {
        const mod = modOf(scores[k]);
        return h("div",
          h("span.label", k.toUpperCase()),
          h("strong", scores[k] ?? "—"),
          mod !== null ? diceChip(mod >= 0 ? `+${mod}` : `${mod}`, parseExpr(`1d20${mod >= 0 ? "+" : ""}${mod}`)) : h("span", ""));
      })),
    props.length > 0 && h("div.sb-props", props.map((p) => {
      const m = p.match(/^(Saving Throws|Skills|Senses|Languages|Damage \w+|Condition Immunities|Challenge|Proficiency Bonus)\s+(.*)$/i);
      return h("p", m ? [h("strong", m[1]), " ", ...inline(m[2])] : inline(p));
    })),
    sections.filter(([, items]) => items.length).map(([title, items]) => h("div.sb-section",
      h("h5", title), items.map(entry))),
  );
}
