// The sheet at the table: a phone-first view of the same character.
//
// Edit mode (sheet.js) is for building the character; this is for playing it.
// Everything is read at a glance and changed with a tap: HP from a keypad,
// resources as pips, coin and item counts with steppers, conditions as
// toggles. Tapping a number rolls it. It shares SheetView's saving, so a tap
// writes into `this.sheet` and the debounced save sends it; HP and rests go
// through the server's endpoints, which apply the rules there.

import { api, h, toast, sign, bottomSheet, keypad, askQty, backCloses, worth } from "./util.js";
import { StashView } from "./stash.js";
import { d, fmtMod, parseExpr, formatExpr, rollExpr } from "./dice.js";
import { SheetView, ABILITIES, ABILITY_NAMES, SKILLS, CONDITIONS, COINS } from "./sheet.js";

// Rolls remembered per character on this device.
const ROLLS_KEPT = 100;

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
    this.screenEl = h("div.roll-screen", { hidden: true, role: "dialog", "aria-modal": "true" });
    const el = h("div.play", this.head, this.panel, this.screenEl, this.tabbar);
    this.el = el;
    this.shadow = structuredClone(this.sheet);  // the sheet as it was before the latest change
    this.undoStack = [];
    this.combat = loadCombat(this.id);
    // Leaving full screen with back changes the button.
    const onFullscreen = () => (el.isConnected ? this.renderHead() : document.removeEventListener("fullscreenchange", onFullscreen));
    document.addEventListener("fullscreenchange", onFullscreen);
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

  // -- undo -------------------------------------------------------------------
  // Every change made here can be taken back: the sheet as it was before is kept,
  // and undoing saves it again. The latest change is offered as an Undo on the toast.

  changed(reason) {
    if (!this.undoing) this.remember(reason, this.shadow);
    this.shadow = structuredClone(this.sheet);
    super.changed(reason);
  }

  remember(reason, before) {
    this.undoStack.push({ reason, sheet: before });
    if (this.undoStack.length > 30) this.undoStack.shift();
    toast(capFirst(reason), false, { label: "Undo", run: () => this.undo() });
  }

  undo() {
    const last = this.undoStack.pop();
    if (!last) return;
    this.sheet = last.sheet;
    this.undoing = true;
    this.changed(`undid ${last.reason}`);
    this.undoing = false;
    this.rerender();
    const more = this.undoStack.length > 0;
    toast(`Undid ${last.reason}`, false, more ? { label: "Undo more", run: () => this.undo() } : null);
  }

  // HP and rests go through the server; the answer replaces the sheet in place.
  async action(method, url, body, reason) {
    await this.flush();
    const before = structuredClone(this.sheet);
    try {
      const r = await api(method, url, body);
      Object.assign(this, { sheet: r.sheet, derived: r.derived, log: r.log, files: r.files || this.files });
      this.shadow = structuredClone(this.sheet);
      if (reason) this.remember(reason, before);
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
    const max = n(d.hp_max);
    const pct = max ? Math.max(0, Math.min(100, (100 * hp.current) / max)) : 0;
    const level = hp.current === 0 ? "down" : pct <= 25 ? "low" : pct <= 50 ? "mid" : "ok";
    this.head.replaceChildren(...[
      h("div.play-title",
        h("a.play-back", { href: "#/characters", title: "Characters", "aria-label": "Back to characters" }, "‹"),
        h("div.play-name",
          h("strong", s.name),
          h("span.muted", [s.species, classes].filter(Boolean).join(" · ") || `Level ${d.level}`)),
        this.status,
        document.fullscreenEnabled && h("button.icon.keep-enabled.fullscreen", {
          title: document.fullscreenElement ? "Leave full screen" : "Full screen",
          "aria-label": document.fullscreenElement ? "Leave full screen" : "Full screen",
          onclick: () => this.toggleFullscreen(),
        }, fullscreenIcon(!!document.fullscreenElement)),
        h("button.icon.keep-enabled", { title: "Dice", "aria-label": "Dice tray", onclick: () => this.tray() }, "🎲"),
        h("button.icon.keep-enabled", { title: "Edit the full sheet", "aria-label": "Edit the full sheet", onclick: () => this.edit() }, "✎"),
      ),
      h("div.play-vitals",
        h(`button.hp-pill.${level}`, {
          onclick: () => this.hpSheet(), disabled: !this.canEdit, "aria-label": `Hit points ${hp.current} of ${max}. Change`,
        },
          h("span.hp-fill", { style: `width:${pct}%` }),
          h("span.hp-label", "HP"),
          h("span.hp-now", hp.current), h("span.hp-max", `/${max}`),
          hp.temp > 0 && h("span.hp-temp", `+${hp.temp}`)),
        vital("AC", n(d.ac)),
        vital("Init", sign(n(d.initiative)), () => this.roll("Initiative", d.initiative)),
        vital("Speed", n(d.speed)),
      ),
      this.combat && this.combatBar(),
      s.statuses.some((st) => st.active) && h("div.play-statuses",
        s.statuses.filter((st) => st.active).map((st) => h("button.chip.status.on", {
          title: `${st.name} is on: see it on Main`, onclick: () => this.showTab("main"),
        }, st.name, this.statusClock(st) && h("small", ` ${this.statusClock(st)}`)))),
      s.conditions.length > 0 && h("div.play-conditions",
        s.conditions.map((c) => h("button.chip.condition", { onclick: () => this.conditionSheet(), disabled: !this.canEdit }, c))),
    ].filter(Boolean));
  }

  // Hide the browser's bars (and the phone's) while the sheet is in use; back or the button leaves.
  async toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen({ navigationUI: "hide" });
    } catch {
      toast("This browser won't go full screen here.", true);
    }
    this.renderHead();
  }

  showTab(key) {
    this.tab = key;
    this.renderPanel();
    this.renderTabs();
    window.scrollTo({ top: 0 });
  }

  renderTabs() {
    this.tabbar.replaceChildren(...TABS
      .filter(([key]) => key !== "spells" || this.sheet.spells.length || this.slotResources().length)
      .map(([key, label, icon]) => h("button", {
        className: key === this.tab ? "active" : "", "aria-current": key === this.tab ? "page" : undefined,
        onclick: () => { this.tab = key; this.renderPanel(); this.renderTabs(); window.scrollTo({ top: 0 }); },
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
    const combat = this.combat;
    return [
      dying && this.deathSaves(),
      this.canEdit && !combat && h("button.start-combat", { onclick: () => this.startCombat() }, "⚔ Start combat"),
      combat && this.grapplingRow(),
      combat && this.statusCards(),
      combat && this.attacksBlock(),
      combat && otherResources.length > 0 && block("Resources", otherResources.map((r) => this.resource(r))),
      h("div.play-abilities", ABILITIES.map((a) => h("button.play-ability", {
        onclick: () => this.roll(`${ABILITY_NAMES[a]} check`, d.modifiers[a]), "aria-label": `${ABILITY_NAMES[a]} check ${sign(d.modifiers[a])}`,
      },
        h("span.ab-name", a.toUpperCase()),
        h("span.ab-mod", sign(d.modifiers[a])),
        h("span.ab-score", n(d.scores?.[a]) || s.abilities[a] || 10)))),
      h("div.play-strip",
        mini("Prof", sign(d.proficiency_bonus)),
        mini("Passive Perc.", n(d.passive_perception)),
        d.spell_save_dc !== undefined && mini("Spell DC", n(d.spell_save_dc)),
        d.spell_attack !== undefined && mini("Spell atk", sign(n(d.spell_attack)), () => this.roll("Spell attack", d.spell_attack))),
      !combat && this.attacksBlock(),
      !combat && this.statusCards(),
      !combat && otherResources.length > 0 && block("Resources", otherResources.map((r) => this.resource(r))),
      block("Saving throws", h("div.play-saves", ABILITIES.map((a) => h("button.play-save", {
        className: `play-save${s.save_proficiencies.includes(a) ? " prof" : ""}`,
        onclick: () => this.roll(`${ABILITY_NAMES[a]} save`, d.saves[a]),
      }, h("span", a.toUpperCase()), h("strong", sign(n(d.saves[a]))))))),
      this.canEdit && h("div.play-actions",
        h("button", { onclick: () => this.conditionSheet() }, "Conditions…"),
        h("button", { onclick: () => this.rest(false) }, "Short rest"),
        h("button", { onclick: () => this.rest(true) }, "Long rest")),
    ];
  }

  attacksBlock() {
    const s = this.sheet, d = this.derived;
    const athletics = sign(n(d.skills.athletics));
    return block("Attacks", [
      s.attacks.map((a, i) => h("button.play-row.play-attack", { onclick: () => this.attack(i) },
        h("span.row-main", h("strong", a.name || "Attack"), a.notes && h("small.muted", a.notes)),
        h("span.badge-num", sign(n(d.attacks[i].to_hit))),
        d.attacks[i].text && h("span.row-side", d.attacks[i].text))),
      h("div.maneuvers",
        h("button", { onclick: () => this.contest("grapple") }, "Grapple", h("small", ` Athletics ${athletics}`)),
        h("button", { onclick: () => this.contest("shove") }, "Shove", h("small", ` Athletics ${athletics}`))),
    ]);
  }

  // -- statuses -----------------------------------------------------------------
  // Each is a card: off, a button to start it (saying what it spends); on, what it's
  // doing, how long it has run in combat, and End.

  statusCards() {
    const list = this.sheet.statuses;
    if (!list.length) return null;
    return block("Statuses", h("div.status-cards", list.map((st) => {
      const r = this.statusResource(st);
      const left = r && `${r.max - r.used}/${r.max} left`;
      const effects = this.statusEffects(st);
      if (!st.active) {
        return h("button.choice.status-card", { disabled: !this.canEdit, onclick: () => this.toggleStatus(st) },
          h("span.choice-name", `Start ${st.name}`),
          h("span.choice-detail", [r && `spends 1 ${r.name} (${left})`, effects, st.duration && `${st.duration} rounds`]
            .filter(Boolean).join(" · ")));
      }
      return h("div.status-card.on",
        h("div.status-main",
          h("span.choice-name", st.name, st.note && h("small", ` ${st.note}`)),
          h("span.choice-detail", [this.statusClock(st), effects, left].filter(Boolean).join(" · "))),
        this.canEdit && h("button", { onclick: () => this.toggleStatus(st) }, "End"));
    })));
  }

  statusResource(st) {
    return st.resource && this.sheet.resources.find((x) => x.name.toLowerCase() === st.resource.toLowerCase());
  }

  // "+2 STR damage, +1 AC" from the bonuses that count while it's on.
  statusEffects(st) {
    const name = st.name.toLowerCase();
    const bonuses = [...this.sheet.bonuses, ...this.sheet.inventory.filter((it) => it.equipped).flatMap((it) => it.bonuses || [])];
    return bonuses.filter((b) => b.during && b.during.toLowerCase() === name && b.active)
      .map((b) => `${typeof b.amount === "number" ? sign(b.amount) : `+${b.amount}`} ${targetLabel(b.target)}`).join(", ");
  }

  // "round 3 of 10" in combat, for a status started this fight; past its duration it
  // says so, and stays on until it's ended.
  statusClock(st) {
    const since = this.combat?.since?.[st.name];
    if (!st.active || since === undefined) return "";
    const round = this.combat.round - since + 1;
    if (!st.duration) return `round ${round}`;
    return round > st.duration ? `round ${round} · lasts ${st.duration}` : `round ${round} of ${st.duration}`;
  }

  // -- combat -------------------------------------------------------------------
  // A layout for the fight: a round tally, how long statuses have run, and whether
  // you're grappling something. It doesn't roll, end or limit anything on its own.
  // Kept on this device (combat:<id>), so a reload carries on.

  startCombat() {
    this.combat = { round: 1, since: {} };
    for (const st of this.sheet.statuses) if (st.active) this.combat.since[st.name] = 1;
    saveCombat(this.id, this.combat);
    this.rerender();
  }

  nextTurn() {
    const c = this.combat;
    c.round += 1;
    saveCombat(this.id, c);
    this.rerender();
  }

  endCombat() {
    this.combat = null;
    saveCombat(this.id, null);
    this.rerender();
  }

  combatBar() {
    const c = this.combat;
    return h("div.combat-bar",
      h("span.combat-round", "⚔ Round ", h("strong", c.round)),
      this.canEdit && h("button.primary", { onclick: () => this.nextTurn() }, "Next turn ▸"),
      this.canEdit && h("button", { onclick: () => this.endCombat() }, "End"));
  }

  // A reminder while grappling something, in combat, until it's let go.
  grapplingRow() {
    const c = this.combat;
    return c.grappling && h("div.grappling",
      h("span", h("strong", "Grappling")),
      h("button", { onclick: () => { c.grappling = false; saveCombat(this.id, c); this.renderPanel(); } }, "Release"));
  }

  // -- grapple and shove ----------------------------------------------------------
  // Athletics against the target's Athletics or Acrobatics.

  contest(kind) {
    const athletics = this.derived.skills.athletics;
    const mod = n(athletics);
    const a = d(20), b = d(20);
    const label = kind === "grapple" ? "Grapple" : "Shove";
    const c = this.combat;
    const grappled = () => {
      if (c) {
        c.grappling = true;
        saveCombat(this.id, c);
        this.renderPanel();
      }
      this.closeScreen();
    };
    this.screen(label, [
      h("div.big-total", a + mod, h("small", " Athletics")),
      breakdown([["d20", String(a)], ...partRows(athletics)], a + mod),
      h("div.adv-line",
        h("div", h("span", "Advantage"), h("strong", Math.max(a, b) + mod)),
        h("div", h("span", "Disadvantage"), h("strong", Math.min(a, b) + mod))),
      h("p.muted", kind === "grapple"
        ? "The target resists with Athletics or Acrobatics. If you win, it's grappled: its speed is 0 until it escapes."
        : "The target resists with Athletics or Acrobatics. If you win, knock it prone or push it 5 feet away."),
    ], kind === "grapple"
      ? [h("button", { onclick: () => this.closeScreen() }, "It got away"), h("button.primary", { onclick: grappled }, "Grappled")]
      : [h("button.primary", { onclick: () => this.closeScreen() }, "Done")]);
    this.record({ label, summary: `Athletics ${a + mod}`, detail: `d20: ${a} ${fmtMod(mod)} · 2nd d20: ${b} → ${b + mod}`, flag: "" });
  }

  skillsTab() {
    const s = this.sheet, d = this.derived;
    return [
      h("div.play-strip",
        mini("Passive Perc.", n(d.passive_perception)),
        mini("Passive Insight", 10 + n(d.skills.insight)),
        mini("Passive Invest.", 10 + n(d.skills.investigation))),
      block("Skills", h("div.play-list", Object.entries(SKILLS).map(([skill, ab]) => {
        const mark = s.skill_expertise.includes(skill) ? "◆" : s.skill_proficiencies.includes(skill) ? "●" : "";
        return h("button.play-row.skill", {
          className: `play-row skill${mark ? " prof" : ""}`, onclick: () => this.roll(cap(skill), d.skills[skill]),
        },
          h("span.prof-mark", { title: mark === "◆" ? "Expertise" : mark ? "Proficient" : "" }, mark),
          h("span.row-main", cap(skill), h("small.muted", ` ${ab.toUpperCase()}`)),
          h("span.badge-num", sign(n(d.skills[skill]))));
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
        d.spell_save_dc !== undefined && mini("Spell DC", n(d.spell_save_dc)),
        d.spell_attack !== undefined && mini("Spell atk", sign(n(d.spell_attack)), () => this.roll("Spell attack", d.spell_attack)),
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
        Object.assign(this, { sheet: r.sheet, derived: r.derived, log: r.log, files: r.files || this.files });
        // The stash changed too, which undo can't take back: start over from here.
        this.shadow = structuredClone(this.sheet);
        this.undoStack = [];
        this.rerender();  // what's equipped changes AC and the rest
      },
    }));
  }

  // An item: its notes and bonuses, equipping and attuning it, and putting it in the stash.
  itemSheet(item) {
    const toggle = (key, label) => h("label.play-toggle",
      h("input", {
        type: "checkbox", checked: item[key], disabled: !this.canEdit,
        onchange: (e) => {
          item[key] = e.target.checked;
          this.changed(`${item.name} ${e.target.checked ? "" : "un"}${key}`);  // the save brings back the new AC etc.
          this.renderPanel();
        },
      }), label);
    const bonuses = item.bonuses || [];
    const dialog = bottomSheet(item.name || "Item",
      item.notes ? h("p", item.notes) : h("p.muted", "No notes. Add them in Edit."),
      item.value_gp > 0 && h("p.muted.small", `Worth ${worth(item.value_gp)}${item.qty !== 1 ? ` each, ${worth(item.value_gp * item.qty)} in all` : ""}`),
      bonuses.length > 0 && h("p.muted.small", bonuses.map((b) => `${typeof b.amount === "number" ? sign(b.amount) : b.amount} ${b.target}`).join(", ")
        + (item.requires_attunement ? " · needs attunement" : "")),
      h("div.item-toggles", toggle("equipped", "Equipped"), item.requires_attunement && toggle("attuned", "Attuned")),
      this.canEdit && h("button.wide", {
        onclick: async () => {
          dialog.close();
          const qty = await askQty(`Put ${item.name} in the party stash`, item.qty, "Put in the stash");
          if (qty) this.stash().move("stash", { [item.name]: qty });
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
      this.files.length > 0 && block("Paper sheet", h("ul.files", this.files.map((f) =>
        h("li", h("a", { href: f.url, target: "_blank", rel: "noopener" }, f.name))))),
      block("Rolls", this.historyBox(ROLLS_KEPT),
        h("button.link.muted.small", { onclick: () => this.clearRolls() }, "Clear")),
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
      h("p.hp-summary", h("strong", hp.current), ` / ${n(this.derived.hp_max)}`, hp.temp ? h("span.hp-temp", ` +${hp.temp} temp`) : ""),
      ...keypad((n) => (amount = n)),
      h("div.hp-buttons",
        h("button.danger-solid", { onclick: go((n) => this.action("POST", `/api/characters/${this.id}/hp`, { delta: -n }, `${n} damage`)) }, "Damage"),
        h("button.ok-solid", { onclick: go((n) => this.action("POST", `/api/characters/${this.id}/hp`, { delta: n }, `healed ${n}`)) }, "Heal"),
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

  // Start or end a status. Starting one spends a use of its resource (Rage), and
  // Concentration asks what it's on. Bonuses during it count once the save comes back.
  toggleStatus(st) {
    if (!st.active) {
      if (/concentrat/i.test(st.name)) {
        const on = prompt("Concentrating on?", st.note || "");
        if (on === null) return;
        st.note = on.trim();
      }
      const r = st.resource && this.sheet.resources.find((x) => x.name.toLowerCase() === st.resource.toLowerCase());
      if (r) {
        if (r.used >= r.max && !confirm(`No ${r.name} left. Start ${st.name} anyway?`)) return;
        r.used = Math.min(r.max, r.used + 1);
      }
    }
    st.active = !st.active;
    if (this.combat) {
      if (st.active) this.combat.since[st.name] = this.combat.round;
      else delete this.combat.since[st.name];
      saveCombat(this.id, this.combat);
    }
    this.changed(`${st.name} ${st.active ? "on" : "off"}`);
    this.renderHead();
    this.renderPanel();
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
    this.action("POST", `/api/characters/${this.id}/rest`, { long }, long ? "long rest" : "short rest");
  }

  // -- dice -------------------------------------------------------------------
  // Rolls take over the screen: a check shows its result at once; an attack
  // first asks what's being chosen (advantage, Reckless Attack, Rage…), then
  // rolls to hit, then on a hit rolls damage.

  // Show the roll screen: a title, a body, and the buttons along the bottom.
  screen(title, body, actions) {
    this.screenEl.replaceChildren(
      h("div.screen-top", h("h2", title),
        h("button.icon", { "aria-label": "Close", onclick: () => this.closeScreen() }, "×")),
      h("div.screen-body", ...[body].flat(Infinity).filter(Boolean)),
      h("div.screen-actions", ...actions.filter(Boolean)));
    if (this.screenEl.hidden) {
      this.screenEl.hidden = false;
      document.body.classList.add("screen-open");
      this.onKey = (e) => { if (e.key === "Escape") this.closeScreen(); };
      document.addEventListener("keydown", this.onKey);
      // Back (a swipe) closes the screen, not the sheet.
      this.releaseBack = backCloses(() => this.closeScreen(true));
    }
    this.screenEl.scrollTop = 0;
    this.screenEl.querySelector(".screen-body").classList.add("pop");
  }

  // `byBack`: the browser's back closed it, so there's no history entry left to take off.
  closeScreen(byBack = false) {
    if (this.screenEl.hidden) return;
    this.screenEl.hidden = true;
    document.body.classList.remove("screen-open");
    document.removeEventListener("keydown", this.onKey);
    if (!byBack) this.releaseBack?.();
    this.releaseBack = null;
  }

  // A d20 roll. A second d20 is shown alongside for advantage or disadvantage.
  // `bonus` is a number, or {total, parts} from the server, whose parts are shown.
  roll(label, bonus) {
    const mod = n(bonus);
    const a = d(20), b = d(20);
    const flag = a === 20 ? "crit" : a === 1 ? "fumble" : "";
    const detail = `d20: ${a} ${fmtMod(mod)}`.trim();
    this.screen(label, [
      h("div.big-total", { className: `big-total${flag ? ` ${flag}` : ""}` }, a + mod),
      breakdown([["d20", String(a), flag === "crit" ? "natural 20" : flag === "fumble" ? "natural 1" : ""], ...partRows(bonus)], a + mod),
      h("div.adv-line",
        h("div", h("span", "Advantage"), h("strong", Math.max(a, b) + mod)),
        h("div", h("span", "Disadvantage"), h("strong", Math.min(a, b) + mod))),
      h("div.roll-detail.muted", `2nd d20: ${b}`),
    ], [
      h("button", { onclick: () => this.roll(label, bonus) }, "Roll again"),
      h("button.primary", { onclick: () => this.closeScreen() }, "Done"),
    ]);
    this.record({ label, summary: String(a + mod), detail: `${detail} · 2nd d20: ${b} → ${b + mod}`, flag });
  }

  // Statuses that change this attack's numbers (Rage): offered as toggles before rolling.
  attackStatuses(i) {
    const a = this.sheet.attacks[i];
    const bonuses = [...this.sheet.bonuses, ...this.sheet.inventory.filter((it) => it.equipped).flatMap((it) => it.bonuses || [])];
    const names = new Set(bonuses.filter((b) => b.during && /^(attack|damage)/.test(b.target)).map((b) => b.during.toLowerCase()));
    return a ? this.sheet.statuses.filter((st) => names.has(st.name.toLowerCase())) : [];
  }

  // Step 1: what's chosen for this attack. Choices are kept per attack until the page is left,
  // so a second swing (Extra Attack) starts from the first's.
  attack(i) {
    const a = this.sheet.attacks[i], da = this.derived.attacks[i];
    if (!a || !da) return;
    this.choices ||= new Map();
    const c = this.choices.get(i) || { adv: "", options: new Set() };
    this.choices.set(i, c);
    const options = (da.options || []).map((k) => ({ k, o: this.sheet.attack_options[k], v: this.derived.attack_options[k] }));
    const chosen = options.filter(({ k }) => c.options.has(k));
    const toHit = n(da.to_hit) + chosen.reduce((t, x) => t + x.v.to_hit, 0);
    const extraDamage = chosen.reduce((t, x) => t + x.v.damage, 0);
    const toggle = (on, label, detail, onclick, note) => h("button.choice", {
      className: `choice${on ? " on" : ""}`, "aria-pressed": String(on), onclick,
    }, h("span.choice-name", label), detail && h("span.choice-detail", detail), note && h("span.choice-note", note));
    const effect = (o, v) => [o.advantage, v.to_hit ? `${sign(v.to_hit)} to hit` : "", v.damage ? `${sign(v.damage)} damage` : ""]
      .filter(Boolean).join(", ");
    const statuses = this.attackStatuses(i);
    this.screen(a.name || "Attack", [
      h("div.attack-summary",
        h("div", h("span.muted", "To hit"), h("strong", sign(toHit))),
        h("div", h("span.muted", "Damage"), h("strong", da.text || "—"),
          extraDamage !== 0 && h("small", `${sign(extraDamage)} from ${chosen.filter((x) => x.v.damage).map((x) => x.o.name).join(", ")}`))),
      h("div.choice-row",
        toggle(c.adv === "advantage", "Advantage", "", () => { c.adv = c.adv === "advantage" ? "" : "advantage"; this.attack(i); }),
        toggle(c.adv === "disadvantage", "Disadvantage", "", () => { c.adv = c.adv === "disadvantage" ? "" : "disadvantage"; this.attack(i); })),
      options.length > 0 && h("h3", "Options"),
      options.map(({ k, o, v }) => toggle(c.options.has(k), o.name, effect(o, v), () => {
        if (c.options.has(k)) c.options.delete(k); else c.options.add(k);
        this.attack(i);
      }, o.note)),
      statuses.length > 0 && h("h3", "Statuses"),
      statuses.map((st) => toggle(st.active, st.name, st.active ? "on" : `off${st.resource ? ` · starting it spends ${st.resource}` : ""}`,
        async () => {
          if (!this.canEdit) return;
          this.toggleStatus(st);
          await this.flush();  // the server works out what it adds
          this.attack(i);
        })),
      a.notes && h("p.muted.small", a.notes),
    ], [
      h("button", { onclick: () => this.damage(i, false, "") }, "Damage only"),
      h("button.primary", { onclick: () => this.rollToHit(i) }, "Roll to hit"),
    ]);
  }

  // Step 2: the attack roll, with advantage or disadvantage from the choices (both cancel out).
  rollToHit(i) {
    const a = this.sheet.attacks[i], da = this.derived.attacks[i];
    const c = this.choices.get(i);
    const chosen = (da.options || []).filter((k) => c.options.has(k)).map((k) => ({ o: this.sheet.attack_options[k], v: this.derived.attack_options[k] }));
    const sources = { advantage: [], disadvantage: [] };
    if (c.adv) sources[c.adv].push("chosen");
    for (const { o } of chosen) if (o.advantage) sources[o.advantage].push(o.name);
    const mode = sources.advantage.length && !sources.disadvantage.length ? "advantage"
      : sources.disadvantage.length && !sources.advantage.length ? "disadvantage" : "";
    const toHit = n(da.to_hit) + chosen.reduce((t, x) => t + x.v.to_hit, 0);
    const r1 = d(20), r2 = d(20);
    const kept = mode === "advantage" ? Math.max(r1, r2) : mode === "disadvantage" ? Math.min(r1, r2) : r1;
    const flag = kept === 20 ? "crit" : kept === 1 ? "fumble" : "";
    const label = `${a.name || "Attack"}${chosen.length ? ` (${chosen.map((x) => x.o.name).join(", ")})` : ""}`;
    const dice = mode ? h("span.dice-pair", h("strong", kept), h("s", kept === r1 ? r2 : r1)) : String(r1);
    const why = sources[mode]?.filter((x) => x !== "chosen").join(", ");
    const notes = chosen.map((x) => x.o.note).filter(Boolean);
    this.screen(label, [
      h("div.big-total", { className: `big-total${flag ? ` ${flag}` : ""}` }, kept + toHit, h("small", " to hit")),
      breakdown([
        [mode ? `d20, ${mode}` : "d20", dice, why],
        ...partRows(da.to_hit),
        ...chosen.filter((x) => x.v.to_hit).map((x) => [x.o.name, x.v.to_hit]),
      ], kept + toHit),
      flag === "crit" && h("p.crit-note", "Natural 20: a critical hit."),
      flag === "fumble" && h("p.fumble-note", "Natural 1: a miss."),
      !mode && h("div.roll-detail.muted", `2nd d20: ${r2} → ${r2 + toHit}`),
      sources.advantage.length > 0 && sources.disadvantage.length > 0 && h("p.muted.small", "Advantage and disadvantage cancel out."),
      notes.map((t) => h("p.reminder", t)),
    ], [
      h("button", { onclick: () => this.closeScreen() }, flag === "fumble" ? "Done" : "Miss"),
      flag !== "fumble" && h("button.primary", { onclick: () => this.damage(i, flag === "crit", label) }, flag === "crit" ? "Crit! Roll damage" : "Hit: roll damage"),
    ]);
    this.record({ label, summary: `to hit ${kept + toHit}`,
      detail: `d20: ${mode ? `${r1}, ${r2} (${mode})` : r1} ${fmtMod(toHit)}`, flag });
  }

  // Step 3: damage that always applies, plus the options' damage on the main part.
  // Situational damage (Sneak Attack, Charge, a smite) is offered as buttons, spending
  // a resource where it says so. `label` is set when this follows a to-hit roll in the history.
  damage(i, crit, label) {
    const a = this.sheet.attacks[i], da = this.derived.attacks[i];
    const c = this.choices.get(i);
    const chosen = (da.options || []).filter((k) => c.options.has(k)).map((k) => ({ o: this.sheet.attack_options[k], v: this.derived.attack_options[k] }));
    // A part's dice, then each modifier (and, on the main part, the options' damage), as lines.
    const rollPart = (part, extra = []) => {
      const type = part.type || "damage";
      const expr = part.dice && parseExpr(part.dice);
      const r = expr?.dice.length ? rollExpr(expr, { crit }) : null;
      const rows = [];
      if (r) rows.push([part.situational || type, String(r.total), `${r.detail}${part.situational ? ` ${type}` : ""}`]);
      rows.push(...partRows(part.bonus), ...extra);
      const total = Math.max(0, rows.reduce((t, [, v]) => t + Number(v), 0));
      const mods = rows.slice(r ? 1 : 0).reduce((t, [, v]) => t + Number(v), 0);
      return { total, type, rows, label: part.situational, detail: `${r ? r.detail : ""}${mods ? ` ${fmtMod(mods)}` : ""}`.trim() };
    };
    const regular = da.damage.filter((p) => !p.situational);
    const optionRows = chosen.filter((x) => x.v.damage).map((x) => [x.o.name, x.v.damage]);
    const rolled = regular.map((p, j) => rollPart(p, j === 0 ? optionRows : []));
    const extras = da.damage.filter((p) => p.situational);
    const added = new Set();
    const fresh = !label;
    label = label || `${a.name || "Attack"} damage`;
    const render = () => {
      const total = rolled.reduce((t, x) => t + x.total, 0);
      const each = rolled.map((x) => `${x.total} ${x.type}${x.label ? ` (${x.label})` : ""}`).join(" + ");
      this.screen(a.name || "Attack", [
        h("div.big-total.damage", total, h("small", ` damage${crit ? " · critical" : ""}`)),
        breakdown(rolled.flatMap((x) => x.rows), total),
        crit && h("p.muted.small", "Critical: the dice are doubled."),
        extras.some((_, k) => !added.has(k)) && h("h3", "If it applies"),
        h("div.extras", extras.map((p, k) => !added.has(k) && h("button.choice", {
          onclick: () => {
            if (p.spends && !this.spend(p.spends, p.situational)) return;
            added.add(k);
            rolled.push(rollPart(p));
            render();
          },
        }, h("span.choice-name", `+ ${p.situational}`),
          h("span.choice-detail", `${p.dice}${p.bonus.total ? sign(p.bonus.total) : ""} ${p.type}${p.spends ? ` · spends ${p.spends}` : ""}`)))),
      ], [
        h("button", { onclick: () => this.attack(i) }, "Attack again"),
        h("button.primary", { onclick: () => this.closeScreen() }, "Done"),
      ]);
      const summary = `${total} damage${rolled.length > 1 ? ` (${each})` : ` ${rolled[0]?.type || ""}`.trimEnd()}`;
      const detail = rolled.map((x) => x.detail).join(" · ") + (crit ? " (crit)" : "");
      if (fresh && !this.damageRecorded) {
        this.record({ label, summary, detail, flag: "" });
        this.damageRecorded = true;
      } else if (fresh) {
        this.amendRoll({ summary, detail });
      } else {
        const hit = this.rolls?.[0];
        this.amendRoll({ summary: `${hit?.summary.split(" · ")[0]} · ${summary}`, detail: `${hit?.detail.split(" · ")[0]} · ${detail}` });
      }
    };
    this.damageRecorded = false;
    render();
  }

  // Use up one of a resource by name; asks first when none are left. False if not spent.
  spend(name, what) {
    const r = this.sheet.resources.find((x) => x.name.toLowerCase() === name.toLowerCase());
    if (!r) return true;
    if (r.used >= r.max && !confirm(`No ${r.name} left. Add ${what} anyway?`)) return false;
    r.used = Math.min(r.max, r.used + 1);
    this.changed(`${what}: ${r.name} ${r.max - r.used}/${r.max}`);
    this.renderPanel();
    return true;
  }

  // -- roll history ---------------------------------------------------------------
  // Kept on this device only, per character: the latest ROLLS_KEPT.

  get rollsKey() {
    return `rolls:${this.id}`;
  }

  loadRolls() {
    if (this.rolls) return;
    try {
      this.rolls = JSON.parse(localStorage.getItem(this.rollsKey)) || [];
    } catch {
      this.rolls = [];  // Private browsing or a corrupt entry: start empty.
    }
  }

  record(roll) {
    this.loadRolls();
    this.rolls = [{ ...roll, created_at: new Date().toISOString() }, ...this.rolls].slice(0, ROLLS_KEPT);
    this.saveRolls();
  }

  // Change the latest roll (situational damage added to it after the fact).
  amendRoll(changes) {
    this.loadRolls();
    if (!this.rolls.length) return;
    this.rolls[0] = { ...this.rolls[0], ...changes };
    this.saveRolls();
  }

  saveRolls() {
    try {
      localStorage.setItem(this.rollsKey, JSON.stringify(this.rolls));
    } catch {
      // Storage full or blocked: the roll still shows until the page is left.
    }
    this.showHistory();
  }

  clearRolls() {
    if (!confirm("Clear this character's roll history on this device?")) return;
    this.rolls = [];
    try {
      localStorage.removeItem(this.rollsKey);
    } catch {
      // Nothing stored to clear.
    }
    this.showHistory();
  }

  // Redraw every open roll list (the tray's, the Notes tab's).
  showHistory() {
    for (const el of document.querySelectorAll(".roll-history")) {
      el.replaceChildren(rollList(this.rolls || [], Number(el.dataset.limit) || 50));
    }
  }

  historyBox(limit) {
    this.loadRolls();
    return h("div.roll-history", { dataset: { limit: String(limit) } }, rollList(this.rolls, limit));
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

}

// -- helpers -----------------------------------------------------------------

// A computed number: {total, parts} from the server, or a plain number.
const n = (v) => (v && typeof v === "object" ? v.total : v ?? 0);
// Four corners pointing out (go full screen) or in (leave it).
function fullscreenIcon(on) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "20");
  svg.setAttribute("height", "20");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", on
    ? "M9 3v6H3M15 3v6h6M9 21v-6H3M15 21v-6h6"
    : "M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2.2");
  path.setAttribute("stroke-linecap", "round");
  svg.append(path);
  return svg;
}

// A sum laid out one part per line, then the total: rows are [label, value, note], where a
// number is a modifier (shown signed) and text is a die's result (shown as it is).
function breakdown(rows, total) {
  return h("div.breakdown",
    rows.map(([label, value, note]) => h("div.bd-row",
      h("span.bd-label", label, note && h("small", ` ${note}`)),
      h("span.bd-value", typeof value === "number" ? sign(value) : value))),
    h("div.bd-row.bd-total", h("span.bd-label"), h("span.bd-value", total)));
}

// The parts of one of the server's numbers ({total, parts}) as breakdown rows; a plain number is one row.
function partRows(v) {
  if (v && typeof v === "object") return v.parts.filter((p) => p.value).map((p) => [p.label, p.value]);
  return v ? [["Bonus", v]] : [];
}

const capFirst = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// "damage:str" -> "STR damage", "save" -> "saves", "ac" -> "AC".
function targetLabel(t) {
  const [what, which] = t.split(":");
  const names = { ac: "AC", hp_max: "max HP", speed: "speed", initiative: "initiative", attack: "to hit", damage: "damage",
    save: "saves", skill: "skills", check: "checks", passive_perception: "passive Perception", spell_dc: "spell DC",
    spell_attack: "spell attack", score: "", score_set: "" };
  const base = names[what] ?? what;
  return which ? `${which.length <= 3 ? which.toUpperCase() : which} ${base}`.trim() : base;
}

function loadCombat(id) {
  try {
    return JSON.parse(localStorage.getItem(`combat:${id}`));
  } catch {
    return null;
  }
}

function saveCombat(id, state) {
  try {
    if (state) localStorage.setItem(`combat:${id}`, JSON.stringify(state));
    else localStorage.removeItem(`combat:${id}`);
  } catch {
    // Storage blocked: combat still works until the page is left.
  }
}

const cap = (s) => s.replace(/\b\w/g, (c) => c.toUpperCase());
function rollList(rolls, limit) {
  if (!rolls.length) return h("p.empty", "No rolls yet.");
  return h("ul.rolls", rolls.slice(0, limit).map((r) => h("li", { className: r.flag ? `roll-${r.flag}` : "" },
    h("div.roll-line", h("span.roll-what", r.label), h("strong", r.summary)),
    h("div.roll-meta", r.detail, " · ", when(r.created_at)))));
}

// "14:05" today, else "Oct 27 14:05".
function when(iso) {
  const t = new Date(iso);
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
