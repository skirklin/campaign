// Encounters: fights the assistant put together, saved in the campaign apart from the
// conversation they were made in.
//   - The card: a ```encounter block in an answer ({"id": n}) shows the saved fight, its
//     difficulty and every stat block, built from the world as it is now, with a button to run it.
//   - The tracker (#/campaigns/<slug>/encounters/<id>): the fight being run. Initiative for the
//     monsters (rolled) and the party (typed in), then the turn order: hit points, conditions
//     with their rules a tap away, legendary actions that come back on the creature's turn,
//     legendary resistance, the lair's turn on initiative 20, and the round. The state is
//     saved on the server, so a reload or another device carries on.
//   - The list (the Encounters tab): every fight prepared, and how each stands.

import { api, h, toast } from "./util.js";
import { parseExpr, rollExpr } from "./dice.js";
import { statBlockFromData, legendaryUses } from "./markdown.js";
import { CONDITIONS } from "./conditions.js";

const mod = (score) => (Number.isFinite(score) ? Math.floor((score - 10) / 2) : 0);
const averageHp = (hp) => Number(String(hp || "").match(/^\s*(\d+)/)?.[1]) || null;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const trackerHref = (slug, id) => `#/campaigns/${slug}/encounters/${id}`;

// -- the card in an answer ----------------------------------------------------------

// A card is drawn again on every frame while its answer streams in, so the fight is fetched
// once and kept a few seconds: long enough for the stream, short enough to show a fight
// that has started since.
const fetched = new Map();
function getEncounter(slug, id) {
  const key = `${slug}/${id}`;
  const hit = fetched.get(key);
  if (hit && Date.now() - hit.at < 5000) return hit.promise;
  const promise = api("GET", `/api/campaigns/${slug}/encounters/${id}`);
  fetched.set(key, { at: Date.now(), promise });
  return promise;
}

export function encounterFence(slug) {
  return (body) => {
    let ref;
    try {
      ref = JSON.parse(body);
    } catch {
      return h("div.encounter-card.pending", "Putting the encounter together…");  // still streaming in
    }
    const card = h("section.encounter-card", h("p.muted.small", "Putting the encounter together…"));
    if (!Number.isInteger(ref?.id)) {
      card.replaceChildren(h("p.muted.small", "This encounter wasn't saved. Ask for it again to get one you can run."));
      return card;
    }
    getEncounter(slug, ref.id).then(
      (e) => card.replaceChildren(...cardBody(e, slug)),
      () => card.replaceChildren(h("p.muted.small", "This encounter has been deleted.")),
    );
    return card;
  };
}

function difficultyLine(f) {
  const party = f.party ? `${f.party.size} characters, level ${f.party.level}` : "no party on file";
  return [
    `${plural(f.monsters, "monster")} · ${f.xp.toLocaleString()} XP`,
    f.adjusted_xp != null ? ` (${f.adjusted_xp.toLocaleString()} adjusted, ×${f.multiplier})` : "",
    ` · ${party}`,
    f.xp_complete ? "" : " · some XP unknown",
  ].join("");
}

function runLabel(state) {
  return state?.started ? `Resume · round ${state.round}` : "Run this fight";
}

function cardBody(e, slug) {
  const f = e.fight;
  const head = h("header",
    h("h4", e.title),
    f.difficulty && h(`span.difficulty.${f.difficulty}`, f.difficulty),
    h("span.muted.small", difficultyLine(f)));
  const thresholds = f.thresholds && h("p.muted.small.thresholds",
    Object.entries(f.thresholds).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(" · "));
  const actions = h("div.encounter-actions",
    h("a.button.primary", { href: trackerHref(slug, e.id) }, runLabel(e.state)));
  return [head, thresholds, actions, ...f.creatures.map((c) => creatureDetails(c, slug, f.creatures.length === 1))];
}

// One kind of creature in the fight: how many, its numbers, and its stat block to open.
function creatureDetails(c, slug, open) {
  const sb = c.stat_block;
  if (!sb) {
    return h("div.encounter-creature.missing",
      h("strong", `${c.count} × ${c.asked_for}`),
      h("p.small", "No stat block for this in your material. Look it up in its own book."));
  }
  return h("details.encounter-creature", { open },
    h("summary",
      h("strong", `${c.count} × ${c.name}`),
      h("span.small.muted", [
        `AC ${sb.armor_class}`, `HP ${c.hp}${c.hp_changed ? " (adventure)" : ""}`,
        c.cr && `CR ${c.cr}`, c.xp_each != null && `${c.xp_each.toLocaleString()} XP each`,
      ].filter(Boolean).join(" · ")),
      c.block_from && h("span.small.muted", `${c.block_from.name} stat block`),
      c.note && h("span.small.encounter-note", c.note)),
    statBlockFromData(c.block_from ? c.block_from.name : c.name, { ...sb, hit_points: c.hp }),
    (c.block_from?.id || c.id) && bookText(slug, c.block_from?.id || c.id));
}

// The book text a stat block was read from, fetched when opened: to check the card against.
function bookText(slug, entityId) {
  const body = h("div.book-text", h("p.muted.small", "Finding it in the book…"));
  let loaded = false;
  return h("details.book-text-toggle", {
    ontoggle: async (ev) => {
      if (!ev.target.open || loaded) return;
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

// -- the list (Encounters tab) --------------------------------------------------------

export async function encountersTab(c) {
  const list = await api("GET", `/api/campaigns/${c.slug}/encounters`);
  const rows = h("div.encounter-list");
  const draw = () => rows.replaceChildren(...(list.length ? list.map(row) : [h("div.empty",
    h("p", "No encounters yet."),
    h("p.muted.small", "Ask for a fight in the Ask tab (“build me the fight at the boar's cave”): "
      + "it's saved here, ready to run."))]));
  const row = (e) => h("div.card.encounter-row",
    h("div.encounter-row-head",
      h("a", { href: trackerHref(c.slug, e.id) }, h("strong", e.title)),
      e.difficulty && h(`span.difficulty.${e.difficulty}`, e.difficulty),
      e.state?.started && h("span.badge", `running · round ${e.state.round}`)),
    h("p.small", e.creatures.map((x) => `${x.count} × ${x.name}`).join(" · "),
      e.xp ? h("span.muted", ` · ${e.xp.toLocaleString()} XP`) : ""),
    h("div.encounter-row-foot",
      e.conversation_id
        ? h("a.small", { href: `#/campaigns/${c.slug}/ask/${e.conversation_id}` }, `from “${e.conversation_title || "a conversation"}”`)
        : h("span.small.muted", ""),
      h("span.spacer"),
      h("a.button", { href: trackerHref(c.slug, e.id) }, runLabel(e.state)),
      h("button.danger", {
        onclick: async () => {
          if (!confirm(`Delete “${e.title}”?`)) return;
          try {
            await api("DELETE", `/api/campaigns/${c.slug}/encounters/${e.id}`);
            list.splice(list.indexOf(e), 1);
            draw();
          } catch (err) {
            toast(err.message, true);
          }
        },
      }, "Delete")));
  draw();
  return h("div.encounters", rows);
}

// -- the tracker ---------------------------------------------------------------------

const CONDITION_NAMES = Object.keys(CONDITIONS);
const LAIR = "lair";

// What a creature brings to the turn order: its AC, hit points, legendary actions and resistance.
function creatureFacts(c) {
  const sb = c.stat_block || {};
  const lr = (sb.traits || []).map((t) => t.match(/^Legendary Resistance \((\d+)\/Day\)/i)).find(Boolean);
  return {
    ac: sb.armor_class ? String(sb.armor_class).match(/^\d+/)?.[0] : null,
    hp: averageHp(c.hp),
    legendary: (sb.legendary_actions || []).length ? legendaryUses(sb.legendary_intro) : 0,
    resistance: lr ? Number(lr[1]) : 0,
    dex: mod(sb.dexterity),
  };
}

// Everyone in the fight before it starts: each monster on its own (they're hit one at a time),
// then the party, then the lair's turn if a creature here has lair actions.
function freshState(fight, party) {
  const combatants = [];
  fight.creatures.forEach((c, i) => {
    const facts = creatureFacts(c);
    for (let n = 1; n <= c.count; n++) {
      combatants.push({
        key: `m${i}-${n}`, ref: i, name: c.count > 1 ? `${c.name} ${n}` : c.name, init: null,
        hp: facts.hp, max: facts.hp, conditions: [], legendary_used: 0, resistance_used: 0,
      });
    }
  });
  for (const p of party) combatants.push({ key: `pc${p.id}`, pc: p.id, name: p.name, init: null, conditions: [] });
  if (fight.creatures.some((c) => (c.stat_block?.lair_actions || []).length)) {
    combatants.push({ key: LAIR, lair: true, name: "Lair actions", init: 20, conditions: [] });
  }
  return { started: false, round: 1, turn: 0, combatants };
}

export async function encounterView(slug, id) {
  const base = `/api/campaigns/${slug}/encounters/${id}`;
  const [e, party] = await Promise.all([api("GET", base), api("GET", `/api/campaigns/${slug}/characters`)]);
  const fight = e.fight;
  let state = e.state || freshState(fight, party);
  const body = h("div.tracker-body");
  const title = h("h1", e.title);

  // Saved shortly after each change, so a quick run of taps is one request, but at least once a
  // second while changes keep coming, and at once if the page is hidden or closed (a laptop lid).
  let timer = 0;
  let waitingSince = 0;
  const flush = async () => {
    clearTimeout(timer);
    timer = 0;
    waitingSince = 0;
    try {
      await api("PATCH", base, { state });
    } catch (err) {
      toast(`Not saved: ${err.message}`, true);
    }
  };
  const save = () => {
    clearTimeout(timer);
    waitingSince ||= Date.now();
    timer = setTimeout(flush, Date.now() - waitingSince > 1000 ? 0 : 300);
  };
  const leaving = () => {
    if (!timer) return;
    clearTimeout(timer);
    timer = 0;
    fetch(base, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state }), keepalive: true });
  };
  addEventListener("pagehide", leaving);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") leaving(); });
  // Every change can be undone (this page remembers the states before it) and says what it was
  // in the log, which is kept with the fight so another device shows it too.
  const undo = [];
  const change = (fn, what) => {
    const before = JSON.stringify(state);
    const said = fn() || what;
    undo.push(before);
    if (undo.length > 60) undo.shift();
    if (said) {
      state.log = [...(state.log || []), { round: state.round, text: said }].slice(-60);
    }
    save();
    draw();
  };
  const undoLast = () => {
    const before = undo.pop();
    if (!before) return;
    state = JSON.parse(before);
    save();
    draw();
  };

  const creatureOf = (x) => (x.ref != null ? fight.creatures[x.ref] : null);
  const isDown = (x) => x.ref != null && x.hp != null && x.hp <= 0;

  // -- before the fight: initiative ---------------------------------------------------
  const setup = () => {
    const rollMonsters = () => change(() => {
      // One roll per kind of creature, as the DMG suggests: the goblins act together.
      const rolls = {};
      for (const x of state.combatants) {
        if (x.ref == null) continue;
        const dex = creatureFacts(creatureOf(x)).dex;
        rolls[x.ref] ??= rollExpr(parseExpr(`1d20${dex >= 0 ? "+" : ""}${dex}`)).total;
        x.init = rolls[x.ref];
      }
    });
    const start = () => {
      const missing = state.combatants.filter((x) => x.init == null || Number.isNaN(x.init));
      if (missing.length) return toast(`Initiative still to set: ${missing.map((x) => x.name).join(", ")}`, true);
      change(() => {
        const dex = (x) => (x.ref != null ? creatureFacts(creatureOf(x)).dex : 0);
        // Ties: the lair loses them, then the higher Dexterity goes first.
        state.combatants.sort((a, b) => b.init - a.init || (a.lair ? 1 : b.lair ? -1 : dex(b) - dex(a)));
        state.started = true;
        state.round = 1;
        state.turn = 0;
        state.log = [];
        return "The fight began";
      });
      window.scrollTo({ top: 0 });
    };
    const addName = h("input", { placeholder: "Name (an ally, a late arrival)", "aria-label": "Name" });
    const addInit = h("input", { type: "number", placeholder: "Init", "aria-label": "Initiative", inputmode: "numeric" });
    const add = (ev) => {
      ev.preventDefault();
      const name = addName.value.trim();
      if (!name) return;
      change(() => state.combatants.push({
        key: `x${Date.now()}`, name, init: addInit.value === "" ? null : Number(addInit.value), conditions: [],
      }));
    };
    return [
      h("p.muted", "Set everyone's initiative, then start. Players roll their own; the monsters' you can roll here."),
      h("div.tracker-setup-actions", h("button", { onclick: rollMonsters }, "Roll for the monsters")),
      h("div.tracker-setup", state.combatants.map((x) => h("div.setup-row",
        h("span.setup-name", x.name, x.pc ? h("span.badge", "party") : x.lair ? h("span.badge", "lair") : ""),
        h("input", {
          type: "number", value: x.init ?? "", placeholder: "Init", inputmode: "numeric", "aria-label": `${x.name} initiative`,
          oninput: (ev) => { x.init = ev.target.value === "" ? null : Number(ev.target.value); save(); },
        }),
        h("button.icon", {
          title: `Leave ${x.name} out`, "aria-label": `Leave ${x.name} out`,
          onclick: () => change(() => state.combatants.splice(state.combatants.indexOf(x), 1)),
        }, "×")))),
      h("form.inline-form.tracker-add", { onsubmit: add }, addName, addInit, h("button", { type: "submit" }, "Add")),
      h("button.primary.tracker-start", { onclick: start }, "Start the fight"),
    ];
  };

  // -- during the fight ---------------------------------------------------------------
  // Monsters of one kind on the same count act together (group initiative): one turn, one block.
  const units = () => {
    const out = [];
    state.combatants.forEach((x, i) => {
      const last = out[out.length - 1];
      const prev = last && state.combatants[last[last.length - 1]];
      if (prev && x.ref != null && prev.ref === x.ref && prev.init === x.init) last.push(i);
      else out.push([i]);
    });
    return out;
  };
  const unitOf = (i) => units().find((u) => u.includes(i)) || [i];
  const advance = (step) => change(() => {
    const us = units();
    if (!us.length) return;
    let k = us.findIndex((u) => u.includes(state.turn));
    for (let n = 0; n < us.length; n++) {
      k += step;
      if (k >= us.length) { k = 0; state.round += 1; }
      if (k < 0) { k = us.length - 1; state.round = Math.max(1, state.round - 1); }
      if (!us[k].every((i) => isDown(state.combatants[i]))) break;  // the fallen don't take turns
    }
    state.turn = us[k][0];
    // Legendary actions come back at the start of the creature's own turn.
    if (step > 0) for (const i of us[k]) state.combatants[i].legendary_used = 0;
    return step > 0 ? `Turn: ${unitName(us[k])}` : `Back to ${unitName(us[k])}`;
  });
  const unitName = (u) => {
    const first = state.combatants[u[0]];
    const c = creatureOf(first);
    return u.length > 1 && c ? `${c.name} ×${u.length}` : first.name;
  };

  // Every kind of creature in the fight, its stat block on screen beside the turn order (on a
  // laptop; under it on a phone), each foldable. Built once: the blocks don't change in a fight.
  const wide = matchMedia("(min-width: 1100px)");
  const sheets = fight.creatures.map((c, i) => {
    const facts = creatureFacts(c);
    const el = h("details.creature-sheet", { open: wide.matches },
      h("summary",
        h("strong", c.count > 1 ? `${c.name} ×${c.count}` : c.name),
        h("span.small.muted", [facts.ac && `AC ${facts.ac}`, c.hp && `HP ${c.hp}`, c.cr && `CR ${c.cr}`].filter(Boolean).join(" · ")),
        c.note && h("span.small.encounter-note", c.note)),
      c.stat_block
        ? statBlockFromData(c.block_from ? c.block_from.name : c.name, { ...c.stat_block, hit_points: c.hp })
        : h("p.small.muted", "No stat block for this in your material. Look it up in its own book."));
    el.dataset.ref = i;
    return el;
  });
  const fold = (open) => sheets.forEach((el) => { el.open = open; });
  const panel = h("aside.tracker-panel",
    h("div.tracker-panel-head",
      h("h3", "In this fight"),
      h("span.spacer"),
      h("button.link.small", { onclick: () => fold(true) }, "Unfold all"),
      h("button.link.small", { onclick: () => fold(false) }, "Fold all")),
    h("div.creature-sheets", sheets));
  // Whose turn it is: their stat block lit up, opened and brought into view.
  const showSheet = (ref, scroll) => {
    sheets.forEach((el) => el.classList.toggle("current", Number(el.dataset.ref) === ref));
    const el = sheets[ref];
    if (!el) return;
    el.open = true;
    if (scroll) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const conditionInfo = new Map();  // combatant key -> the condition whose rules are showing
  const opened = new Set();  // rows opened by a tap, beside the current turn's
  const selected = new Set();  // monsters ticked for one amount of damage or healing at once

  const hurt = (x, delta) => {
    const was = x.hp ?? 0;
    x.hp = Math.max(0, Math.min(x.max ?? Infinity, was + delta));
    return `${x.name} ${delta < 0 ? `took ${-delta}` : `healed ${delta}`} (${was} → ${x.hp})`;
  };

  const hpReadout = (x) => {
    const pct = x.max ? Math.max(0, Math.min(100, (100 * (x.hp ?? 0)) / x.max)) : 0;
    return h("div.hp-readout",
      h("strong", `${x.hp ?? "?"}`), h("span.muted", ` / ${x.max ?? "?"}`),
      h("div.hp-bar", h("span", { style: `width:${pct}%` })));
  };

  const hpControl = (x) => {
    const amount = h("input", { type: "number", min: 0, placeholder: "HP", inputmode: "numeric", title: "How much damage or healing",
      "aria-label": `${x.name} damage or healing` });
    const apply = (sign) => {
      const n = Number(amount.value);
      if (!n) return amount.focus();
      change(() => hurt(x, sign * n));
    };
    return h("div.hp-control", amount,
      h("button.danger", { onclick: () => apply(-1), title: "Take that much damage" }, "Hit"),
      h("button", { onclick: () => apply(1), title: "Heal that much" }, "Heal"));
  };

  const chips = (x) => {
    const shown = conditionInfo.get(x.key);
    return x.conditions.map((n) => h(`button.condition-chip${shown === n ? ".active" : ""}`, {
      title: CONDITIONS[n] ? CONDITIONS[n][0] : n,
      onclick: (ev) => { ev.stopPropagation(); if (shown === n) conditionInfo.delete(x.key); else conditionInfo.set(x.key, n); draw(); },
    }, n));
  };
  const conditionPicker = (x) => h("select", {
    "aria-label": `Add a condition to ${x.name}`,
    onchange: (ev) => {
      let name = ev.target.value;
      if (name === "other") name = prompt("Condition or effect (Concentrating, Blessed, Hexed…)")?.trim() || "";
      ev.target.value = "";
      if (name && !x.conditions.includes(name)) change(() => { x.conditions.push(name); return `${x.name}: ${name}`; });
    },
  }, h("option", { value: "" }, "+ condition"),
    ...CONDITION_NAMES.filter((n) => !x.conditions.includes(n)).map((n) => h("option", { value: n }, n)),
    h("option", { value: "other" }, "other…"));
  const conditionRule = (x) => {
    const shown = conditionInfo.get(x.key);
    return shown && h("div.condition-info",
      h("strong", shown),
      CONDITIONS[shown] ? h("ul", CONDITIONS[shown].map((line) => h("li", line))) : h("p.muted.small", "Not one of the rules' conditions: whatever the effect says."),
      h("button.link", {
        onclick: () => change(() => {
          x.conditions = x.conditions.filter((n) => n !== shown);
          conditionInfo.delete(x.key);
          return `${x.name}: no longer ${shown}`;
        }),
      }, `End ${shown}`));
  };

  const ticks = (label, total, used, set, x) => h("span.tracker-ticks", { title: label },
    h("span.small.muted", `${label} `),
    Array.from({ length: Math.min(total, 9) }, (_, i) => h("input", {
      type: "checkbox", checked: i < used, "aria-label": `${label} ${i + 1}`,
      onclick: (ev) => ev.stopPropagation(),
      onchange: (ev) => change(() => { set(ev.target.checked ? Math.max(used, i + 1) : i); return ev.target.checked ? `${x.name}: ${label.toLowerCase()} used` : null; }),
    })));

  const selectBox = (keys, label) => h("input.select-box", {
    type: "checkbox", checked: keys.length > 0 && keys.every((k) => selected.has(k)), disabled: !keys.length,
    "aria-label": label, title: "Tick to damage or heal several at once",
    onclick: (ev) => ev.stopPropagation(),
    onchange: (ev) => { for (const k of keys) (ev.target.checked ? selected.add(k) : selected.delete(k)); draw(); },
  });

  // One combatant: a line of what matters at a glance, opening to its controls on its turn or a tap.
  const row = (x, i, current, inGroup) => {
    const c = creatureOf(x);
    const facts = c ? creatureFacts(c) : null;
    const down = isDown(x);
    const open = (current && !down) || opened.has(x.key);
    const lair = x.lair && fight.creatures.find((cr) => (cr.stat_block?.lair_actions || []).length)?.stat_block;
    const stats = x.pc && party.find((p) => p.id === x.pc)?.stats;
    const toggle = () => { if (opened.has(x.key)) opened.delete(x.key); else opened.add(x.key); draw(); };
    const legendary = facts && (facts.legendary || facts.resistance) ? h("span.tracker-legendary",
      facts.legendary ? ticks("Legendary", facts.legendary, x.legendary_used || 0, (n) => { x.legendary_used = n; }, x) : "",
      facts.resistance ? ticks("Resistance", facts.resistance, x.resistance_used || 0, (n) => { x.resistance_used = n; }, x) : "") : "";
    return h(`div.combatant${current ? ".current" : ""}${down ? ".down" : ""}${x.pc ? ".pc" : ""}${open ? ".open" : ""}${inGroup ? ".member" : ""}`,
      { id: `c-${x.key}` },
      h("div.combatant-line", { onclick: x.lair ? null : toggle, title: x.lair ? "" : "Tap to open or close" },
        c ? selectBox([x.key], `Select ${x.name}`) : h("span.select-gap"),
        inGroup ? h("span.init.quiet", "") : h("span.init", x.init ?? "–"),
        h("strong.combatant-name", x.name),
        x.pc && h("span.badge", "party"),
        stats && h("span.small.muted", `AC ${stats.ac} · PP ${stats.passive_perception}`),
        facts?.ac && !inGroup && h("span.small.muted", `AC ${facts.ac}`),
        down && h("span.badge.down", "down"),
        h("span.line-chips", chips(x)),
        h("span.spacer"),
        legendary,
        c && hpReadout(x)),
      open && h("div.combatant-controls",
        c && hpControl(x),
        !x.lair && conditionPicker(x),
        // The stat block comes with the current turn on a phone (below), so a link to it is only for the rest.
        c && !(current && !wide.matches) && h("button.link.small.sheet-link", { onclick: () => showSheet(x.ref, true) }, "Stat block")),
      conditionRule(x),
      // The lair's options in full on its turn; otherwise just how many, to keep the order short.
      lair && (current
        ? h("div.lair-actions", lair.lair_intro && h("p.small.muted", lair.lair_intro),
          h("ul", (lair.lair_actions || []).map((a) => h("li.small", a))))
        : h("p.small.muted.lair-actions", `${plural((lair.lair_actions || []).length, "option")}, on this count`)),
      // On a phone the current monster's stat block comes with its turn, rather than at the page's end.
      current && !wide.matches && c?.stat_block && !inGroup &&
        h("details.inline-sheet", { open: true }, h("summary.small", `${c.name} stat block`),
          statBlockFromData(c.block_from ? c.block_from.name : c.name, { ...c.stat_block, hit_points: c.hp })));
  };

  // Several of one kind on one count: a block with its own header (select them all, how many stand).
  const group = (u, current) => {
    const members = u.map((i) => state.combatants[i]);
    const c = creatureOf(members[0]);
    const standing = members.filter((x) => !isDown(x)).length;
    return h(`div.combatant-group${current ? ".current" : ""}`,
      h("div.group-head",
        selectBox(members.filter((x) => !isDown(x)).map((x) => x.key), `Select every ${c.name}`),
        h("span.init", members[0].init ?? "–"),
        h("strong", `${c.name} ×${members.length}`),
        h("span.small.muted", `AC ${creatureFacts(c).ac ?? "?"} · ${standing} standing`),
        h("span.spacer"),
        h("button.link.small.sheet-link", { onclick: () => showSheet(members[0].ref, true) }, "Stat block")),
      members.map((x) => row(x, state.combatants.indexOf(x), false, true)),
      current && !wide.matches && c?.stat_block &&
        h("details.inline-sheet", { open: true }, h("summary.small", `${c.name} stat block`),
          statBlockFromData(c.block_from ? c.block_from.name : c.name, { ...c.stat_block, hit_points: c.hp })));
  };

  // Ticked monsters take one amount at once: a fireball, with Half for those who saved.
  const bulkBar = () => {
    const picked = state.combatants.filter((x) => selected.has(x.key));
    if (!picked.length) return "";
    const amount = h("input", { type: "number", min: 0, placeholder: "HP", inputmode: "numeric", "aria-label": "Damage or healing for the ticked" });
    const apply = (factor) => {
      const n = Number(amount.value);
      if (!n) return amount.focus();
      const each = Math.sign(factor) * Math.floor(n * Math.abs(factor));  // half rounds down, as the rules say
      change(() => picked.map((x) => hurt(x, each)).join("; "));
      selected.clear();
      draw();
    };
    return h("div.bulk-bar",
      h("strong", `${picked.length} ticked`), amount,
      h("button.danger", { onclick: () => apply(-1) }, "Hit"),
      h("button.danger", { onclick: () => apply(-0.5), title: "Half the damage, rounded down: for those who saved" }, "Half"),
      h("button", { onclick: () => apply(1) }, "Heal"),
      h("button.link", { onclick: () => { selected.clear(); draw(); } }, "Clear"));
  };

  const running = () => {
    const us = units();
    const turnUnit = unitOf(state.turn);
    const last = undo.length ? (state.log || []).slice(-1)[0] : null;
    return [
      h("div.tracker-bar",
        h("div.tracker-round", h("span.small.muted", "Round"), h("strong", state.round)),
        h("div.tracker-now", h("span.small.muted", "Turn"), h("strong", unitName(turnUnit))),
        h("button", { onclick: undoLast, disabled: !undo.length, title: last ? `Undo: ${last.text}` : "Nothing to undo" }, "Undo"),
        h("button", { onclick: () => advance(-1), title: "Back a turn" }, "‹ Back"),
        h("button.primary", { onclick: () => advance(1) }, "Next turn")),
      bulkBar(),
      h("div.turn-order", us.map((u) => {
        const current = u.includes(state.turn);
        return u.length > 1 ? group(u, current) : row(state.combatants[u[0]], u[0], current, false);
      })),
      (state.log || []).length > 0 && h("details.tracker-log", { open: wide.matches },
        h("summary", "Log"),
        h("ol", [...state.log].reverse().slice(0, 30).map((l) => h("li", h("span.muted.small", `R${l.round} `), l.text)))),
      h("div.tracker-end",
        h("button.danger", {
          onclick: () => {
            if (!confirm("End the fight? The turn order, hit points and conditions are cleared.")) return;
            state = freshState(fight, party);
            undo.length = 0;
            api("PATCH", base, { reset: true }).catch((err) => toast(err.message, true));
            draw();
          },
        }, "End the fight")),
    ];
  };

  const draw = () => {
    body.replaceChildren(...(state.started ? running() : setup()));
    if (!state.started) return showSheet(-1, false);
    body.querySelector(".combatant.current")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const now = state.combatants[state.turn];
    showSheet(now?.ref ?? -1, wide.matches && now?.ref != null);
  };

  const rename = h("button.icon", {
    title: "Rename this encounter", "aria-label": "Rename this encounter",
    onclick: async () => {
      const name = prompt("Name this encounter", e.title)?.trim();
      if (!name || name === e.title) return;
      try {
        e.title = (await api("PATCH", base, { title: name })).title;
        title.textContent = e.title;
      } catch (err) {
        toast(err.message, true);
      }
    },
  }, "✎");
  draw();
  return h("section.page.tracker",
    h("div.page-head", h("a", { href: `#/campaigns/${slug}/encounters` }, "← Encounters")),
    h("div.tracker-title", title, rename,
      fight.difficulty && h(`span.difficulty.${fight.difficulty}`, fight.difficulty)),
    h("p.muted.small", difficultyLine(fight),
      e.conversation_id ? [" · ", h("a", { href: `#/campaigns/${slug}/ask/${e.conversation_id}` }, "the conversation it came from")] : ""),
    h("div.tracker-layout", h("div.tracker-main", body), panel));
}
