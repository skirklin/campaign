// The sheet at the table: a phone-first view of the same character.
//
// Edit mode (sheet.js) is for building the character; this is for playing it.
// Everything is read at a glance and changed with a tap: HP from a keypad,
// resources as pips, coin and item counts with steppers, conditions as
// toggles. Tapping a number rolls it. It shares SheetView's saving, so a tap
// writes into `this.sheet` and the debounced save sends it; HP and rests go
// through the server's endpoints, which apply the rules there.

import { api, h, toast, sign, bottomSheet, keypad, askQty } from "./util.js";
import { StashView } from "./stash.js";
import { d, fmtMod, parseExpr, formatExpr, rollExpr } from "./dice.js";
import { SheetView, ABILITIES, ABILITY_NAMES, SKILLS, CONDITIONS, COINS } from "./sheet.js";

const TABS = [
  ["main", "Main", "⚔"],
  ["skills", "Skills", "🎯"],
  ["spells", "Spells", "✦"],
  ["gear", "Gear", "🎒"],
  ["notes", "Notes", "✎"],
];

// The open tab per character, so a reload after a save lands where you were.
const openTab = new Map();

export class PlayView extends SheetView {
  get tab() {
    return openTab.get(this.id) || "main";
  }

  set tab(t) {
    openTab.set(this.id, t);
  }

  render() {
    this.status = h("span.save-status");
    this.head = h("div.play-head");
    this.panel = h("div.play-panel");
    this.tabbar = h("nav.play-tabs", { "aria-label": "Sheet sections" });
    this.rollBox = h("div.roll", { hidden: true, onclick: () => (this.rollBox.hidden = true) });
    const el = h("div.play", this.head, this.panel, this.rollBox, this.tabbar);
    this.el = el;
    this.rerender();
    keepAwake();
    return el;
  }

  // Derived numbers came back from a save: redraw, unless someone's typing in the panel.
  updateDerived() {
    this.renderHead();
    const typing = this.panel.contains(document.activeElement) && document.activeElement.matches("input, textarea");
    if (!typing) this.renderPanel();
  }

  refresh() {}

  lock() {
    this.el.classList.add("read-only");
  }

  rerender() {
    this.renderHead();
    this.renderPanel();
    this.renderTabs();
  }

  // HP and rests go through the server; the answer replaces the sheet in place.
  async action(method, url, body) {
    await this.flush();
    try {
      const r = await api(method, url, body);
      Object.assign(this, { sheet: r.sheet, derived: r.derived, log: r.log });
      this.rerender();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async edit() {
    await this.flush();
    setMode("edit");
    this.el.replaceWith(new SheetView(this.data()).el);
  }

  // -- header: who, HP, AC, conditions; always on screen -----------------------

  renderHead() {
    const s = this.sheet, d = this.derived, hp = s.hp;
    const classes = s.classes.map((c) => `${c.name} ${c.level}`).join(" / ");
    const pct = hp.max ? Math.max(0, Math.min(100, (100 * hp.current) / hp.max)) : 0;
    const level = hp.current === 0 ? "down" : pct <= 25 ? "low" : pct <= 50 ? "mid" : "ok";
    this.head.replaceChildren(...[
      h("div.play-title",
        h("div.play-name",
          h("strong", s.name),
          h("span.muted", [s.species, classes].filter(Boolean).join(" · ") || `Level ${d.level}`)),
        this.status,
        h("button.icon.keep-enabled", { title: "Dice", "aria-label": "Dice tray", onclick: () => this.tray() }, "🎲"),
        h("button.icon.keep-enabled", { title: "Edit the full sheet", "aria-label": "Edit the full sheet", onclick: () => this.edit() }, "✎"),
      ),
      h("div.play-vitals",
        h(`button.hp-pill.${level}`, {
          onclick: () => this.hpSheet(), disabled: !this.canEdit, "aria-label": `Hit points ${hp.current} of ${hp.max}. Change`,
        },
          h("span.hp-fill", { style: `width:${pct}%` }),
          h("span.hp-label", "HP"),
          h("span.hp-now", hp.current), h("span.hp-max", `/${hp.max}`),
          hp.temp > 0 && h("span.hp-temp", `+${hp.temp}`)),
        vital("AC", s.ac),
        vital("Init", sign(d.initiative), () => this.roll("Initiative", d.initiative)),
        vital("Speed", s.speed),
      ),
      s.conditions.length > 0 && h("div.play-conditions",
        s.conditions.map((c) => h("button.chip.condition", { onclick: () => this.conditionSheet(), disabled: !this.canEdit }, c))),
    ].filter(Boolean));
  }

  renderTabs() {
    this.tabbar.replaceChildren(...TABS
      .filter(([key]) => key !== "spells" || this.sheet.spells.length || this.slotResources().length)
      .map(([key, label, icon]) => h("button", {
        className: key === this.tab ? "active" : "", "aria-current": key === this.tab ? "page" : undefined,
        onclick: () => { this.tab = key; this.rollBox.hidden = true; this.renderPanel(); this.renderTabs(); window.scrollTo({ top: 0 }); },
      }, h("span.tab-icon", { "aria-hidden": "true" }, icon), label)));
  }

  renderPanel() {
    const build = { main: this.main, skills: this.skillsTab, spells: this.spellsTab, gear: this.gear, notes: this.notesTab }[this.tab];
    this.panel.replaceChildren(...[build.call(this)].flat(Infinity).filter(Boolean));
  }

  // -- tabs -----------------------------------------------------------------

  main() {
    const s = this.sheet, d = this.derived;
    const dying = s.hp.current === 0 || s.death_saves.successes > 0 || s.death_saves.failures > 0;
    const otherResources = s.resources.filter((r) => !isSlot(r));
    return [
      dying && this.deathSaves(),
      h("div.play-abilities", ABILITIES.map((a) => h("button.play-ability", {
        onclick: () => this.roll(`${ABILITY_NAMES[a]} check`, d.modifiers[a]), "aria-label": `${ABILITY_NAMES[a]} check ${sign(d.modifiers[a])}`,
      },
        h("span.ab-name", a.toUpperCase()),
        h("span.ab-mod", sign(d.modifiers[a])),
        h("span.ab-score", s.abilities[a] ?? 10)))),
      h("div.play-strip",
        mini("Prof", sign(d.proficiency_bonus)),
        mini("Passive Perc.", d.passive_perception),
        d.spell_save_dc !== undefined && mini("Spell DC", d.spell_save_dc),
        d.spell_attack !== undefined && mini("Spell atk", sign(d.spell_attack), () => this.roll("Spell attack", d.spell_attack))),
      s.attacks.length > 0 && block("Attacks", s.attacks.map((a) => h("button.play-row.attack", { onclick: () => this.attack(a) },
        h("span.row-main", h("strong", a.name || "Attack"), a.notes && h("small.muted", a.notes)),
        a.to_hit && h("span.badge-num", a.to_hit),
        a.damage && h("span.row-side", a.damage)))),
      otherResources.length > 0 && block("Resources", otherResources.map((r) => this.resource(r))),
      block("Saving throws", h("div.play-saves", ABILITIES.map((a) => h("button.play-save", {
        className: `play-save${s.save_proficiencies.includes(a) ? " prof" : ""}`,
        onclick: () => this.roll(`${ABILITY_NAMES[a]} save`, d.saves[a]),
      }, h("span", a.toUpperCase()), h("strong", sign(d.saves[a])))))),
      this.canEdit && h("div.play-actions",
        h("button", { onclick: () => this.conditionSheet() }, "Conditions…"),
        h("button", { onclick: () => this.rest(false) }, "Short rest"),
        h("button", { onclick: () => this.rest(true) }, "Long rest")),
    ];
  }

  skillsTab() {
    const s = this.sheet, d = this.derived;
    return [
      h("div.play-strip",
        mini("Passive Perc.", d.passive_perception),
        mini("Passive Insight", 10 + d.skills.insight),
        mini("Passive Invest.", 10 + d.skills.investigation)),
      block("Skills", h("div.play-list", Object.entries(SKILLS).map(([skill, ab]) => {
        const mark = s.skill_expertise.includes(skill) ? "◆" : s.skill_proficiencies.includes(skill) ? "●" : "";
        return h("button.play-row.skill", {
          className: `play-row skill${mark ? " prof" : ""}`, onclick: () => this.roll(cap(skill), d.skills[skill]),
        },
          h("span.prof-mark", { title: mark === "◆" ? "Expertise" : mark ? "Proficient" : "" }, mark),
          h("span.row-main", cap(skill), h("small.muted", ` ${ab.toUpperCase()}`)),
          h("span.badge-num", sign(d.skills[skill])));
      }))),
    ];
  }

  slotResources() {
    return this.sheet.resources.filter(isSlot);
  }

  spellsTab() {
    const s = this.sheet, d = this.derived;
    const slots = this.slotResources();
    const slotFor = new Map(slots.map((r) => [slotLevel(r), r]).filter(([l]) => l));
    const loose = slots.filter((r) => !slotLevel(r));
    const levels = [...new Set([...s.spells.map((sp) => sp.level), ...slotFor.keys()])].sort((a, b) => a - b);
    this.preparedOnly ??= s.spells.some((sp) => !sp.prepared);
    const shown = (sp) => !this.preparedOnly || sp.prepared || sp.level === 0;
    return [
      h("div.play-strip",
        d.spell_save_dc !== undefined && mini("Spell DC", d.spell_save_dc),
        d.spell_attack !== undefined && mini("Spell atk", sign(d.spell_attack), () => this.roll("Spell attack", d.spell_attack)),
        s.spells.some((sp) => !sp.prepared) && h("label.play-toggle",
          h("input", { type: "checkbox", checked: this.preparedOnly, onchange: (e) => { this.preparedOnly = e.target.checked; this.renderPanel(); } }),
          "Prepared only")),
      loose.length > 0 && block("Slots", loose.map((r) => this.resource(r))),
      levels.map((lvl) => {
        const slot = slotFor.get(lvl);
        const spells = s.spells.filter((sp) => sp.level === lvl && shown(sp)).sort((a, b) => a.name.localeCompare(b.name));
        return block(lvl === 0 ? "Cantrips" : `Level ${lvl}`, [
          slot && this.pips(slot),
          spells.length === 0 && h("p.empty", "None prepared."),
          spells.map((sp) => h("details.play-spell", { className: `play-spell${sp.prepared ? "" : " unprepared"}` },
            h("summary", h("span", sp.name || "Unnamed spell"), !sp.prepared && h("small.muted", "not prepared")),
            sp.notes ? h("p", sp.notes) : h("p.muted", "No notes. Add them in Edit."))),
        ], slot && h("span.muted.small", `${slot.max - slot.used}/${slot.max} slots`));
      }),
    ];
  }

  gear() {
    const s = this.sheet;
    return [
      block("Coin", h("div.play-coins", COINS.map((c) => h("button.play-coin", {
        onclick: () => this.coinSheet(c), disabled: !this.canEdit, "aria-label": `${s.currency[c] || 0} ${c}. Change`,
      }, h("strong", s.currency[c] || 0), h("span", c))))),
      block("Inventory", s.inventory.length === 0 ? h("p.empty", "Nothing carried.") : h("div.play-list", s.inventory.map((item) =>
        h("div.play-row.item",
          h("button.link.row-main", { onclick: () => this.itemSheet(item), "aria-label": `${item.name}: more` },
            h("span", item.name || "Item", item.equipped && h("small.equipped", " equipped"), item.attuned && h("small.equipped", " attuned")),
            item.notes && h("small.muted", item.notes)),
          this.stepper(item.qty, (n) => { item.qty = Math.max(0, n); this.changed(`${item.name} ×${item.qty}`); }))))),
      this.stash().el,
      s.features.length > 0 && block("Features & traits", h("ul.play-bullets", s.features.map((f) => h("li", f)))),
      s.proficiencies.length > 0 && block("Proficiencies & languages", h("p", s.proficiencies.join(", "))),
    ];
  }

  // The party stash, taking into and putting in from this character. Kept across redraws.
  stash() {
    if (!this.canEdit) return { el: null };
    return (this.stashView ||= new StashView(this.campaign.slug, {
      character: this.id,
      beforeMove: () => this.flush(),
      onCharacter: (r) => {
        Object.assign(this, { sheet: r.sheet, derived: r.derived, log: r.log });
        this.renderPanel();
      },
    }));
  }

  itemSheet(item) {
    const dialog = bottomSheet(item.name || "Item",
      item.notes ? h("p", item.notes) : h("p.muted", "No notes. Add them in Edit."),
      this.canEdit && h("button.wide", {
        onclick: async () => {
          dialog.close();
          const n = await askQty(`Put ${item.name} in the party stash`, item.qty, "Put in the stash");
          if (n) this.stash().move("stash", { [item.name]: n });
        },
      }, "Put in the party stash"));
  }

  notesTab() {
    const note = (path, label, rows) => {
      const el = h("textarea", { value: this.sheet[path], rows, "aria-label": label, disabled: !this.canEdit });
      el.addEventListener("change", () => { this.sheet[path] = el.value; this.changed(`edited ${label.toLowerCase()}`); });
      return block(label, el);
    };
    this.worldBox = h("div", h("p.muted", "Loading…"));
    this.loadWorld();
    return [
      note("notes", "Notes", 10),
      note("personality", "Personality", 4),
      block("Rolls", this.historyBox(50)),
      block("In the world", this.worldBox),
      block("History", h("ul.log", this.log.map((l) => h("li", h("time", l.created_at.replace("T", " ").slice(0, 16)), " ", l.reason)))),
    ];
  }

  // -- pieces ---------------------------------------------------------------

  resource(r) {
    return h("div.play-resource",
      h("div.res-head", h("span", r.name || "Resource"), h("span.muted.small", `${r.max - r.used}/${r.max}${r.recharge ? ` · ${r.recharge}` : ""}`)),
      this.pips(r));
  }

  // Filled pips are what's left. Tapping a filled pip spends down to it; an empty one restores up to it.
  pips(r) {
    const left = r.max - r.used;
    const set = (used) => {
      r.used = Math.max(0, Math.min(r.max, used));
      this.changed(`${r.name || "resource"} ${r.max - r.used}/${r.max}`);
      this.renderPanel();
    };
    if (r.max > 20) {
      return h("div.res-counter", this.stepper(left, (n) => set(r.max - n), r.max));
    }
    return h("div.play-pips", Array.from({ length: r.max }, (_, p) => h("button", {
      className: `play-pip${p < left ? " full" : ""}`, disabled: !this.canEdit,
      "aria-label": `${r.name}: ${p < left ? "spend" : "restore"} (${left}/${r.max} left)`,
      onclick: () => set(p < left ? r.max - p : r.max - p - 1),
    })));
  }

  stepper(value, onChange, max = Infinity) {
    let n = value;
    const out = h("span.step-value", n);
    const step = (by) => {
      n = Math.max(0, Math.min(max, n + by));
      out.textContent = n;
      onChange(n);
    };
    return h("span.stepper",
      h("button", { onclick: () => step(-1), disabled: !this.canEdit, "aria-label": "One less" }, "−"),
      out,
      h("button", { onclick: () => step(1), disabled: !this.canEdit, "aria-label": "One more" }, "+"));
  }

  deathSaves() {
    const ds = this.sheet.death_saves;
    const row = (kind, label) => h("div.death-row", h("span", label), [0, 1, 2].map((i) => h("button", {
      className: `death-pip ${kind}${i < ds[kind] ? " full" : ""}`, disabled: !this.canEdit,
      "aria-label": `${label} ${i + 1}`,
      onclick: () => { ds[kind] = i < ds[kind] ? i : i + 1; this.changed("death saves"); this.renderPanel(); },
    })));
    return h("section.play-block.dying",
      h("h2", "Death saves"),
      row("successes", "Successes"), row("failures", "Failures"),
      h("button.wide", { onclick: () => this.roll("Death save", 0) }, "Roll death save"));
  }

  // -- bottom sheets ---------------------------------------------------------

  hpSheet() {
    const hp = this.sheet.hp;
    let amount = 0;
    const go = (fn) => () => { if (!amount) return; dialog.close(); fn(amount); };
    const dialog = bottomSheet("Hit points",
      h("p.hp-summary", h("strong", hp.current), ` / ${hp.max}`, hp.temp ? h("span.hp-temp", ` +${hp.temp} temp`) : ""),
      ...keypad((n) => (amount = n)),
      h("div.hp-buttons",
        h("button.danger-solid", { onclick: go((n) => this.action("POST", `/api/characters/${this.id}/hp`, { delta: -n })) }, "Damage"),
        h("button.ok-solid", { onclick: go((n) => this.action("POST", `/api/characters/${this.id}/hp`, { delta: n })) }, "Heal"),
        h("button", { onclick: go((n) => { this.sheet.hp.temp = n; this.changed(`temp hp ${n}`); this.rerender(); }) }, "Set temp")),
    );
  }

  coinSheet(coin) {
    const cur = this.sheet.currency;
    let amount = 0;
    const go = (dir) => () => {
      if (!amount) return;
      dialog.close();
      const next = (cur[coin] || 0) + dir * amount;
      if (next < 0) return toast(`Only ${cur[coin] || 0} ${coin}.`, true);
      cur[coin] = next;
      this.changed(`${dir > 0 ? "+" : "−"}${amount} ${coin}`);
      this.renderPanel();
    };
    const dialog = bottomSheet(`${coin.toUpperCase()}: ${cur[coin] || 0}`,
      ...keypad((n) => (amount = n)),
      h("div.hp-buttons",
        h("button.danger-solid", { onclick: go(-1) }, "Spend"),
        h("button.ok-solid", { onclick: go(1) }, "Gain"),
        h("button", { onclick: () => { if (!amount) return; dialog.close(); this.stash().move("stash", {}, { [coin]: amount }); } }, "To stash")));
  }

  conditionSheet() {
    const list = this.sheet.conditions;
    const all = [...CONDITIONS, ...list.filter((c) => !CONDITIONS.includes(c))];
    const grid = h("div.condition-grid", all.map((c) => h("button", {
      className: list.includes(c) ? "on" : "", "aria-pressed": String(list.includes(c)),
      onclick: (e) => {
        const i = list.indexOf(c);
        if (i >= 0) list.splice(i, 1); else list.push(c);
        e.currentTarget.classList.toggle("on", i < 0);
        e.currentTarget.setAttribute("aria-pressed", String(i < 0));
        this.changed(`${i >= 0 ? "no longer " : ""}${c}`);
        this.renderHead();
      },
    }, c)));
    bottomSheet("Conditions", grid);
  }

  rest(long) {
    const what = long ? "Long rest: restore HP and everything that recharges on a short or long rest?" : "Short rest: restore everything that recharges on a short rest?";
    if (!confirm(what)) return;
    this.action("POST", `/api/characters/${this.id}/rest`, { long });
  }

  // -- dice -------------------------------------------------------------------

  // A d20 roll. A second d20 is shown alongside for advantage or disadvantage.
  roll(label, mod) {
    const a = d(20), b = d(20);
    const flag = a === 20 ? "crit" : a === 1 ? "fumble" : "";
    const detail = `d20: ${a} ${fmtMod(mod)}`.trim();
    this.showRoll(label, [
      h("div.roll-total", { className: `roll-total${flag ? ` ${flag}` : ""}` }, a + mod),
      h("div.roll-detail", detail),
      h("div.roll-detail.muted", `2nd d20: ${b} → ${b + mod} · adv ${Math.max(a, b) + mod} · dis ${Math.min(a, b) + mod}`),
    ]);
    this.record({ label, summary: String(a + mod), detail: `${detail} · 2nd d20: ${b} → ${b + mod}`, flag });
  }

  attack(a) {
    const toHit = parseInt(a.to_hit, 10);
    const dmg = parseExpr(a.damage);
    const first = d(20);
    const crit = first === 20;
    const flag = crit ? "crit" : first === 1 ? "fumble" : "";
    const parts = [], summary = [], detail = [];
    if (!Number.isNaN(toHit)) {
      const second = d(20);
      parts.push(
        h("div.roll-total", { className: `roll-total${flag ? ` ${flag}` : ""}` }, first + toHit, h("small", " to hit")),
        h("div.roll-detail", `d20: ${first} ${fmtMod(toHit)}${crit ? " · critical!" : ""}`),
        h("div.roll-detail.muted", `2nd d20: ${second} → ${second + toHit}`));
      summary.push(`to hit ${first + toHit}`);
      detail.push(`d20: ${first} ${fmtMod(toHit)} · 2nd d20: ${second} → ${second + toHit}`);
    }
    if (dmg?.dice.length) {
      const r = rollExpr(dmg, { crit });
      const total = Math.max(0, r.total);
      parts.push(h("div.roll-total.damage", total, h("small", ` ${dmg.rest || "damage"}`)),
        h("div.roll-detail", r.detail + (crit ? " (crit: dice doubled)" : "")));
      summary.push(`${total} ${dmg.rest || "damage"}`);
      detail.push(r.detail + (crit ? " (crit)" : ""));
    }
    if (!parts.length) return toast("Add a to-hit bonus or damage dice to this attack in Edit.");
    this.showRoll(a.name || "Attack", parts);
    this.record({ label: a.name || "Attack", summary: summary.join(" · "), detail: detail.join(" · "), flag: Number.isNaN(toHit) ? "" : flag });
  }

  // -- roll history ---------------------------------------------------------------
  // Kept on the server against the character (for its owner and DM; anyone
  // else's rolls on this sheet stay on their screen).

  async loadRolls() {
    if (!this.rollsLoading) {
      this.rollsLoading = api("GET", `/api/characters/${this.id}/rolls`)
        .then((list) => { this.rolls = [...(this.rolls || []).filter((r) => r.pending), ...list]; this.showHistory(); })
        .catch(() => { this.rollsLoading = null; });
    }
    return this.rollsLoading;
  }

  record(roll) {
    const entry = { ...roll, created_at: new Date().toISOString(), pending: true };
    this.rolls = [entry, ...(this.rolls || [])];
    this.showHistory();
    if (!this.canEdit) return;
    api("POST", `/api/characters/${this.id}/rolls`, roll)
      .then((saved) => { Object.assign(entry, saved, { pending: false }); })
      .catch(() => { entry.unsaved = true; this.showHistory(); });
  }

  // Redraw every open roll list (the tray's, the Notes tab's).
  showHistory() {
    for (const el of document.querySelectorAll(".roll-history")) {
      el.replaceChildren(rollList(this.rolls || [], Number(el.dataset.limit) || 50));
    }
  }

  historyBox(limit) {
    const el = h("div.roll-history", { dataset: { limit: String(limit) } }, h("p.muted", "Loading…"));
    if (this.rolls) el.replaceChildren(rollList(this.rolls, limit));
    this.loadRolls();
    return el;
  }

  // The dice tray: build an expression with the buttons, or type one.
  tray() {
    let advantage = "";
    const input = h("input.tray-expr", {
      value: this.trayExpr || "1d20", "aria-label": "Dice to roll", autocomplete: "off", autocapitalize: "off", spellcheck: false,
      // Typing makes the expression whatever's typed: advantage is only what it says.
      oninput: () => { this.trayExpr = input.value; advantage = ""; advBtn.classList.remove("on"); disBtn.classList.remove("on"); },
    });
    const result = h("div.tray-result", h("p.muted", "Tap dice to add them, then Roll. Or type, like 2d6+3 or 4d6kh3."));
    const set = (expr) => { input.value = this.trayExpr = expr; };
    const parsed = () => parseExpr(input.value) || { dice: [], mod: 0, rest: "" };
    const addDie = (sides) => {
      const p = parsed();
      const same = p.dice.find((t) => t.sides === sides && t.n > 0 && !t.keep);
      if (same) same.n += 1; else p.dice.push({ n: 1, sides });
      set(formatExpr(p));
    };
    const addMod = (by) => { const p = parsed(); p.mod += by; set(formatExpr(p)); };
    // Advantage and disadvantage turn the d20 into 2d20 keeping the higher or lower.
    const adv = (kind) => {
      const p = parsed();
      advantage = advantage === kind ? "" : kind;
      let t = p.dice.find((x) => x.sides === 20);
      if (!t) p.dice.unshift((t = { n: 1, sides: 20 }));
      Object.assign(t, advantage ? { n: 2, keep: [kind === "adv" ? "h" : "l", 1] } : { n: 1, keep: undefined });
      set(formatExpr(p));
      advBtn.classList.toggle("on", advantage === "adv");
      disBtn.classList.toggle("on", advantage === "dis");
    };
    const advBtn = h("button", { onclick: () => adv("adv") }, "Adv");
    const disBtn = h("button", { onclick: () => adv("dis") }, "Dis");
    const roll = () => {
      const p = parseExpr(input.value);
      if (!p || !p.dice.length) return toast("Try something like 2d6+3, 8d6 or 2d20kh1+5.", true);
      const r = rollExpr(p);
      const flag = r.natural === 20 ? "crit" : r.natural === 1 ? "fumble" : "";
      const label = p.rest || formatExpr(p);
      result.replaceChildren(
        h("div.roll-label", label),
        h("div", { className: `roll-total${flag ? ` ${flag}` : ""}` }, r.total),
        h("div.roll-detail", r.detail));
      result.classList.remove("pop");
      void result.offsetWidth;
      result.classList.add("pop");
      this.record({ label, summary: String(r.total), detail: r.detail, flag });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); roll(); } });
    bottomSheet("Dice",
      h("div.tray-dice", [4, 6, 8, 10, 12, 20, 100].map((n) => h("button", { onclick: () => addDie(n) }, `d${n}`))),
      h("div.tray-row", input,
        h("button", { onclick: () => addMod(-1), "aria-label": "Minus one" }, "−1"),
        h("button", { onclick: () => addMod(1), "aria-label": "Plus one" }, "+1")),
      h("div.tray-row", advBtn, disBtn, h("button", { onclick: () => { advantage = ""; advBtn.classList.remove("on"); disBtn.classList.remove("on"); set(""); input.focus(); } }, "Clear")),
      h("button.primary.wide.tray-roll", { onclick: roll }, "Roll"),
      result,
      h("h3", "Recent rolls"),
      this.historyBox(8));
  }

  showRoll(label, body) {
    clearTimeout(this.rollTimer);
    this.rollBox.replaceChildren(h("div.roll-label", label), ...body);
    this.rollBox.hidden = false;
    this.rollBox.classList.remove("pop");
    void this.rollBox.offsetWidth;
    this.rollBox.classList.add("pop");
    this.rollTimer = setTimeout(() => (this.rollBox.hidden = true), 8000);
  }
}

// -- helpers -----------------------------------------------------------------

const cap = (s) => s.replace(/\b\w/g, (c) => c.toUpperCase());
function rollList(rolls, limit) {
  if (!rolls.length) return h("p.empty", "No rolls yet.");
  return h("ul.rolls", rolls.slice(0, limit).map((r) => h("li", { className: r.flag ? `roll-${r.flag}` : "" },
    h("div.roll-line", h("span.roll-what", r.label), h("strong", r.summary)),
    h("div.roll-meta", r.detail, " · ", when(r.created_at), r.unsaved && h("span.unsaved", " · not saved")))));
}

// "14:05" today, else "Oct 27 14:05".
function when(iso) {
  const t = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toDateString() === new Date().toDateString() ? time : `${t.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

// "Spell slots (1st)", "2nd level slots", "Pact slots" -> a resource that's a spell slot.
function isSlot(r) {
  return /slot/i.test(r.name);
}

function slotLevel(r) {
  const m = r.name.match(/\b([1-9])(?:st|nd|rd|th)?\b/);
  return m ? Number(m[1]) : 0;
}

function vital(label, value, onclick) {
  return h(onclick ? "button.vital" : "div.vital", { onclick }, h("span.vital-label", label), h("strong", value));
}

function mini(label, value, onclick) {
  return h(onclick ? "button.mini-stat" : "div.mini-stat", { onclick }, h("strong", value), h("span", label));
}

function block(title, body, aside) {
  return h("section.play-block", h("div.block-head", h("h2", title), aside), body);
}

// Which view the sheet opens in, remembered on this device.
export function preferredMode() {
  try {
    return localStorage.getItem("sheet-mode") || "play";
  } catch {
    return "play";
  }
}

export function setMode(mode) {
  try {
    localStorage.setItem("sheet-mode", mode);
  } catch {
    // Private browsing: the choice just isn't remembered.
  }
}

// Keep the phone's screen on while a sheet is open in Play, at the table.
let wakeLock = null;
let watching = false;
function keepAwake() {
  if (!("wakeLock" in navigator) || watching) return;
  watching = true;
  const sync = async () => {
    const want = document.visibilityState === "visible" && document.querySelector(".play");
    if (want && !wakeLock) {
      try {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => (wakeLock = null));
      } catch {
        // Not allowed (battery saver, say): the screen just sleeps as usual.
      }
    } else if (!want && wakeLock) {
      wakeLock.release();
    }
  };
  document.addEventListener("visibilitychange", sync);
  new MutationObserver(sync).observe(document.getElementById("view"), { childList: true });
  setTimeout(sync);
}
