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

  // Saved shortly after each change, so a quick run of taps is one request.
  let timer = 0;
  const save = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        await api("PATCH", base, { state });
      } catch (err) {
        toast(`Not saved: ${err.message}`, true);
      }
    }, 300);
  };
  const change = (fn) => { fn(); save(); draw(); };

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
  const advance = (step) => change(() => {
    const n = state.combatants.length;
    if (!n) return;
    let i = state.turn;
    for (let k = 0; k < n; k++) {
      i += step;
      if (i >= n) { i = 0; state.round += 1; }
      if (i < 0) { i = n - 1; state.round = Math.max(1, state.round - 1); }
      if (!isDown(state.combatants[i])) break;  // the fallen don't take turns
    }
    state.turn = i;
    const now = state.combatants[i];
    if (step > 0 && now.legendary_used) now.legendary_used = 0;  // they come back at the start of its turn
  });

  const open = new Set();  // whose stat block is showing
  const conditionInfo = new Map();  // combatant key -> the condition whose rules are showing

  const hpControl = (x) => {
    const amount = h("input", { type: "number", min: 0, placeholder: "0", inputmode: "numeric", "aria-label": `${x.name} damage or healing` });
    const apply = (sign) => {
      const n = Number(amount.value);
      if (!n) return amount.focus();
      change(() => { x.hp = Math.max(0, Math.min(x.max ?? Infinity, (x.hp ?? 0) + sign * n)); });
    };
    const pct = x.max ? Math.max(0, Math.min(100, (100 * (x.hp ?? 0)) / x.max)) : 0;
    return h("div.hp-control",
      h("div.hp-readout",
        h("strong", `${x.hp ?? "?"}`), h("span.muted", ` / ${x.max ?? "?"} HP`),
        h("div.hp-bar", h("span", { style: `width:${pct}%` }))),
      amount,
      h("button.danger", { onclick: () => apply(-1), title: "Take that much damage" }, "Hit"),
      h("button", { onclick: () => apply(1), title: "Heal that much" }, "Heal"));
  };

  const conditions = (x) => {
    const shown = conditionInfo.get(x.key);
    const pick = h("select", {
      "aria-label": `Add a condition to ${x.name}`,
      onchange: (ev) => {
        let name = ev.target.value;
        if (name === "other") name = prompt("Condition or effect (Concentrating, Blessed, Hexed…)")?.trim() || "";
        ev.target.value = "";
        if (name && !x.conditions.includes(name)) change(() => x.conditions.push(name));
      },
    }, h("option", { value: "" }, "+ condition"),
      ...CONDITION_NAMES.filter((n) => !x.conditions.includes(n)).map((n) => h("option", { value: n }, n)),
      h("option", { value: "other" }, "other…"));
    const info = shown && h("div.condition-info",
      h("strong", shown),
      CONDITIONS[shown] ? h("ul", CONDITIONS[shown].map((line) => h("li", line))) : h("p.muted.small", "Not one of the rules' conditions: whatever the effect says."),
      h("button.link", { onclick: () => change(() => { x.conditions = x.conditions.filter((n) => n !== shown); conditionInfo.delete(x.key); }) }, `End ${shown}`));
    return h("div.conditions",
      h("div.condition-chips",
        x.conditions.map((n) => h(`button.condition-chip${shown === n ? ".active" : ""}`, {
          title: CONDITIONS[n] ? CONDITIONS[n][0] : n,
          onclick: () => { if (shown === n) conditionInfo.delete(x.key); else conditionInfo.set(x.key, n); draw(); },
        }, n)),
        pick),
      info);
  };

  const ticks = (label, total, used, set) => h("span.tracker-ticks", { title: label },
    h("span.small.muted", `${label} `),
    Array.from({ length: Math.min(total, 9) }, (_, i) => h("input", {
      type: "checkbox", checked: i < used, "aria-label": `${label} ${i + 1}`,
      onchange: (ev) => change(() => set(ev.target.checked ? Math.max(used, i + 1) : i)),
    })));

  const row = (x, i) => {
    const c = creatureOf(x);
    const facts = c ? creatureFacts(c) : null;
    const current = i === state.turn;
    const expand = c?.stat_block && h("button.link.small", {
      onclick: () => { if (open.has(x.key)) open.delete(x.key); else open.add(x.key); draw(); },
    }, open.has(x.key) ? "Hide stat block" : "Stat block");
    const lair = x.lair && fight.creatures.find((cr) => (cr.stat_block?.lair_actions || []).length)?.stat_block;
    return h(`div.combatant${current ? ".current" : ""}${isDown(x) ? ".down" : ""}${x.pc ? ".pc" : ""}`, { id: `c-${x.key}` },
      h("div.combatant-head",
        h("span.init", x.init ?? "–"),
        h("strong.combatant-name", x.name),
        x.pc && h("span.badge", "party"),
        facts?.ac && h("span.small.muted", `AC ${facts.ac}`),
        isDown(x) && h("span.badge.down", "down"),
        h("span.spacer"),
        expand),
      c && hpControl(x),
      !x.lair && conditions(x),
      (facts?.legendary || facts?.resistance) ? h("div.tracker-legendary",
        facts.legendary ? ticks("Legendary actions", facts.legendary, x.legendary_used || 0, (n) => { x.legendary_used = n; }) : "",
        facts.resistance ? ticks("Legendary Resistance", facts.resistance, x.resistance_used || 0, (n) => { x.resistance_used = n; }) : "") : "",
      // The lair's options in full on its turn; otherwise just how many, to keep the order short.
      lair && (current
        ? h("div.lair-actions", lair.lair_intro && h("p.small.muted", lair.lair_intro),
          h("ul", (lair.lair_actions || []).map((a) => h("li.small", a))))
        : h("p.small.muted.lair-actions", `${plural((lair.lair_actions || []).length, "option")}, on this count`)),
      open.has(x.key) && statBlockFromData(c.block_from ? c.block_from.name : c.name, { ...c.stat_block, hit_points: c.hp }));
  };

  const running = () => {
    const current = state.combatants[state.turn];
    return [
      h("div.tracker-bar",
        h("div.tracker-round", h("span.small.muted", "Round"), h("strong", state.round)),
        h("div.tracker-now", h("span.small.muted", "Turn"), h("strong", current ? current.name : "–")),
        h("button", { onclick: () => advance(-1), title: "Back a turn" }, "‹"),
        h("button.primary", { onclick: () => advance(1) }, "Next turn")),
      h("div.turn-order", state.combatants.map(row)),
      h("div.tracker-end",
        h("button.danger", {
          onclick: () => {
            if (!confirm("End the fight? The turn order, hit points and conditions are cleared.")) return;
            state = freshState(fight, party);
            api("PATCH", base, { reset: true }).catch((err) => toast(err.message, true));
            draw();
          },
        }, "End the fight")),
    ];
  };

  const draw = () => {
    body.replaceChildren(...(state.started ? running() : setup()));
    if (state.started) body.querySelector(".combatant.current")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
    body);
}
