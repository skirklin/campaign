// Character list and the editable character sheet.
//
// The sheet is built once and edited in place: every input writes into
// `this.sheet` and schedules a debounced save. The server validates and
// returns the derived numbers (modifiers, skills, AC, attacks, DCs), each
// with the parts it adds up from, which are patched into elements marked
// `data-d` so typing never loses focus; clicking one shows its parts. Only
// list sections re-render, when rows are added or removed.
//
// That's Edit mode. Play mode (play.js) is the same sheet laid out for the
// table; the sheet opens in whichever this device used last.

import { api, h, toast, sign, getPath, setPath, debounce } from "./util.js";

export const ABILITIES = ["str", "dex", "con", "int", "wis", "cha"];
export const ABILITY_NAMES = { str: "Strength", dex: "Dexterity", con: "Constitution", int: "Intelligence", wis: "Wisdom", cha: "Charisma" };
export const SKILLS = {
  acrobatics: "dex", "animal handling": "wis", arcana: "int", athletics: "str", deception: "cha",
  history: "int", insight: "wis", intimidation: "cha", investigation: "int", medicine: "wis",
  nature: "int", perception: "wis", performance: "cha", persuasion: "cha", religion: "int",
  "sleight of hand": "dex", stealth: "dex", survival: "wis",
};
export const CONDITIONS = [
  "blinded", "charmed", "deafened", "exhaustion", "frightened", "grappled", "incapacitated", "invisible",
  "paralyzed", "petrified", "poisoned", "prone", "restrained", "stunned", "unconscious", "concentrating",
];
const RECHARGE = ["", "short rest", "long rest", "dawn"];
const ATTACK_ABILITIES = { str: "STR", dex: "DEX", finesse: "Finesse", spell: "Spell", con: "CON", int: "INT", wis: "WIS", cha: "CHA", none: "None" };
// Where a bonus can apply, grouped for the picker (the server's BONUS_TARGETS).
const BONUS_TARGETS = [
  ["Defense & movement", [["ac", "AC"], ["hp_max", "Max HP"], ["speed", "Speed"], ["initiative", "Initiative"]]],
  ["Attacks", [["attack", "All attack rolls"], ...ABILITIES.map((a) => [`attack:${a}`, `${a.toUpperCase()} attack rolls`]),
    ["damage", "All damage"], ...ABILITIES.map((a) => [`damage:${a}`, `${a.toUpperCase()} attack damage`])]],
  ["Saves", [["save", "All saves"], ...ABILITIES.map((a) => [`save:${a}`, `${ABILITY_NAMES[a]} save`])]],
  ["Checks", [["skill", "All skills"], ...ABILITIES.map((a) => [`check:${a}`, `${ABILITY_NAMES[a]} checks`]),
    ...Object.keys(SKILLS).map((k) => [`skill:${k}`, k[0].toUpperCase() + k.slice(1)]), ["passive_perception", "Passive Perception"]]],
  ["Spells", [["spell_dc", "Spell save DC"], ["spell_attack", "Spell attack"]]],
  ["Ability scores", [...ABILITIES.map((a) => [`score:${a}`, `${ABILITY_NAMES[a]} score +`]),
    ...ABILITIES.map((a) => [`score_set:${a}`, `${ABILITY_NAMES[a]} score at least`])]],
];
export const COINS = ["cp", "sp", "ep", "gp", "pp"];
// Common choices made when attacking, ready to add (amounts can be edited after).
const ATTACK_OPTION_PRESETS = {
  "Reckless Attack": { name: "Reckless Attack", applies_to: ["str", "melee"], advantage: "advantage",
    note: "Attacks against you have advantage until your next turn." },
  "Great Weapon Master": { name: "Great Weapon Master", applies_to: ["heavy", "melee"], to_hit: -5, damage: 10 },
  "Sharpshooter": { name: "Sharpshooter", applies_to: ["ranged", "weapon"], to_hit: -5, damage: 10 },
  "Blank": {},
};

// A character lives in exactly one campaign. This page is a view across them:
// your characters, grouped by campaign, and the rest of each of your parties.
export async function characterList() {
  const [chars, campaigns] = await Promise.all([api("GET", "/api/characters"), api("GET", "/api/campaigns")]);
  const byCampaign = (list) => {
    const groups = new Map();
    for (const c of list) {
      if (!groups.has(c.campaign.slug)) groups.set(c.campaign.slug, { campaign: c.campaign, chars: [] });
      groups.get(c.campaign.slug).chars.push(c);
    }
    return [...groups.values()].map((g) => h("div.char-group",
      h("h3", h("a", { href: `#/campaigns/${g.campaign.slug}/party` }, g.campaign.name)),
      h("div.cards", g.chars.map((c) => characterCard(c)))));
  };
  const mine = chars.filter((c) => c.mine);
  const others = chars.filter((c) => !c.mine);
  return h("section.page",
    h("h1", "Characters"),
    mine.length === 0 && h("p.empty", campaigns.length
      ? "No characters yet. Create one in a campaign, or import a JSON sheet."
      : ["A character belongs to a campaign. ", h("a", { href: "#/campaigns" }, "Create or join one"), " first."]),
    byCampaign(mine),
    campaigns.length > 0 && newCharacterForm(campaigns),
    others.length > 0 && h("h2.section-title", "The rest of your parties"),
    byCampaign(others),
  );
}

export function characterCard(c) {
  return h("a.card.char-card", { href: `#/characters/${c.id}` },
    h("strong", c.name),
    h("span.muted", [c.species, c.summary].filter(Boolean).join(" · ") || "—"),
    hpBar(c.hp),
  );
}

// Create or import a character: into `campaign` if given, else one picked from `campaigns`.
export function newCharacterForm(campaigns, campaign = null) {
  const nameInput = h("input", { placeholder: "New character name", required: true, "aria-label": "Character name" });
  const pick = !campaign && h("select", { "aria-label": "Campaign", required: true },
    campaigns.map((c) => h("option", { value: c.slug }, c.name)));
  const slug = () => campaign?.slug || pick.value;
  const open = (r) => (location.hash = `#/characters/${r.id}`);
  const fileInput = h("input", {
    type: "file", accept: ".json,application/json", hidden: true,
    onchange: async () => {
      const fd = new FormData();
      fd.append("file", fileInput.files[0]);
      fd.append("campaign", slug());
      try {
        open(await api("POST", "/api/characters/import", fd));
      } catch (err) {
        toast(err.message, true);
      }
    },
  });
  return h("form.inline-form", {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        open(await api("POST", "/api/characters", { name: nameInput.value.trim(), campaign: slug() }));
      } catch (err) {
        toast(err.message, true);
      }
    },
  }, nameInput, pick || "", h("button.primary", { type: "submit" }, "Create"),
    h("button", { type: "button", onclick: () => fileInput.click() }, "Import JSON…"), fileInput);
}

function hpBar(hp) {
  const pct = hp.max ? Math.max(0, Math.min(100, (100 * hp.current) / hp.max)) : 0;
  const level = pct <= 25 ? "low" : pct <= 50 ? "mid" : "ok";
  return h("div.hpbar", { title: `${hp.current}/${hp.max} HP` },
    h(`div.hpbar-fill.${level}`, { style: `width:${pct}%` }),
    h("span", `${hp.current}/${hp.max}${hp.temp ? ` +${hp.temp}` : ""}`),
  );
}

export async function sheetView(id) {
  const [data, { PlayView, preferredMode }] = await Promise.all([api("GET", `/api/characters/${id}`), import("./play.js")]);
  // A new, empty character opens in Edit: there's nothing to play yet.
  const play = preferredMode() === "play" && data.sheet.classes.length > 0;
  return new (play ? PlayView : SheetView)(data).el;
}

export class SheetView {
  constructor({ id, campaign, can_edit, entity_id, copied_from, sheet, derived, log, files }) {
    this.id = id;
    this.campaign = campaign;
    this.entityId = entity_id;
    this.copiedFrom = copied_from;
    this.canEdit = can_edit;
    this.sheet = sheet;
    this.derived = derived;
    this.log = log;
    this.files = files || [];
    this.reasons = new Set();
    this.queue = Promise.resolve();
    this.sections = {};
    this.scheduleSave = debounce(() => this.flush(), 600);
    this.el = this.render();
    this.updateDerived();
    if (!this.canEdit) this.lock();
    this.loadWorld();
  }

  // -- the character in its campaign's world ---------------------------------

  async loadWorld() {
    if (!this.worldBox) return;
    try {
      const e = await api("GET", `/api/campaigns/${this.campaign.slug}/entities/${this.entityId}`);
      const items = e.timeline;
      this.worldBox.replaceChildren(items.length
        ? h("ul.timeline", items.map((i) => h("li",
            h("span.session-num", i.session === 0 ? "before" : `#${i.session}`), " ",
            i.change === "ended" ? h("s", i.statement) : i.statement)))
        : h("p.muted", "Nothing recorded yet. Session ingests and chats add to this."));
    } catch (err) {
      this.worldBox.replaceChildren(h("p.muted", err.message));
    }
  }

  async copyTo() {
    const others = (await api("GET", "/api/campaigns")).filter((c) => c.slug !== this.campaign.slug);
    if (!others.length) return toast("You're not in any other campaign to copy this character to.", true);
    const pick = h("select", { "aria-label": "Campaign" }, others.map((c) => h("option", { value: c.slug }, c.name)));
    const dialog = h("dialog.copy-dialog",
      h("form", { method: "dialog" },
        h("h3", `Copy ${this.sheet.name} to…`),
        pick,
        h("p.muted.small", "The copy is a separate sheet in that campaign: changes to one don't touch the other."),
        h("div.actions", h("button", { value: "cancel" }, "Cancel"), h("button.primary", { value: "copy" }, "Copy"))));
    dialog.addEventListener("close", async () => {
      dialog.remove();
      if (dialog.returnValue !== "copy") return;
      try {
        const r = await api("POST", `/api/characters/${this.id}/copy`, { campaign: pick.value });
        location.hash = `#/characters/${r.id}`;
      } catch (err) {
        toast(err.message, true);
      }
    });
    document.body.append(dialog);
    dialog.showModal();
  }

  // Someone else's sheet: readable, not editable. Export still works.
  lock() {
    this.el.classList.add("read-only");
    this.status.textContent = "Read only";
    for (const el of this.el.querySelectorAll("input, select, textarea, button")) {
      if (!el.classList.contains("keep-enabled")) el.disabled = true;
    }
  }

  // The scan the sheet was read from (and anything else attached), to check the sheet against.
  filesLine() {
    const input = h("input", { type: "file", accept: ".pdf,.png,.jpg,.jpeg,.webp", hidden: true,
      onchange: async () => {
        const fd = new FormData();
        fd.append("file", input.files[0]);
        try {
          this.replaceWith(await api("POST", `/api/characters/${this.id}/files`, fd));
        } catch (err) {
          toast(err.message, true);
        }
      } });
    const remove = async (f) => {
      if (!confirm(`Remove ${f.name} from this character?`)) return;
      await api("DELETE", `/api/characters/${this.id}/files/${f.id}`);
      this.reload();
    };
    return h("p.files-line.small",
      this.files.length > 0 && h("span.muted", this.files.length > 1 ? "Scans: " : "Scan: "),
      this.files.map((f, i) => [i > 0 && " · ",
        h("a", { href: f.url, target: "_blank", rel: "noopener" }, f.name),
        this.canEdit && h("button.icon.link", { title: `Remove ${f.name}`, "aria-label": `Remove ${f.name}`, onclick: () => remove(f) }, "×")]),
      this.canEdit && [this.files.length > 0 && " · ",
        h("button.link", { onclick: () => input.click() }, this.files.length ? "Attach another" : "Attach the paper sheet (photo or PDF)…"), input],
    );
  }

  // -- saving ----------------------------------------------------------------

  changed(reason) {
    this.reasons.add(reason);
    this.status.textContent = "Unsaved…";
    this.scheduleSave();
  }

  flush() {
    if (!this.reasons.size) return this.queue;
    const reason = [...this.reasons].join(", ");
    this.reasons.clear();
    const body = { sheet: structuredClone(this.sheet), reason };
    // Chain saves so responses can't arrive out of order.
    this.queue = this.queue.then(async () => {
      this.status.textContent = "Saving…";
      try {
        const r = await api("PUT", `/api/characters/${this.id}`, body);
        this.derived = r.derived;
        this.log = r.log;
        this.updateDerived();
        this.refresh("log");
        this.status.textContent = "Saved";
      } catch (err) {
        toast(`Not saved: ${err.message}`, true);
        this.reload();
      }
    });
    return this.queue;
  }

  async action(method, url, body) {
    await this.flush();
    try {
      this.replaceWith(await api(method, url, body));
    } catch (err) {
      toast(err.message, true);
    }
  }

  async reload() {
    this.replaceWith(await api("GET", `/api/characters/${this.id}`));
  }

  replaceWith(data) {
    const fresh = new this.constructor(data);
    this.el.replaceWith(fresh.el);
  }

  data() {
    return {
      id: this.id, campaign: this.campaign, can_edit: this.canEdit, entity_id: this.entityId,
      copied_from: this.copiedFrom, sheet: this.sheet, derived: this.derived, log: this.log,
    };
  }

  async play() {
    await this.flush();
    const { PlayView, setMode } = await import("./play.js");
    setMode("play");
    this.el.replaceWith(new PlayView(this.data()).el);
  }

  updateDerived() {
    for (const el of this.el.querySelectorAll("[data-d]")) {
      const v = getPath(this.derived, el.dataset.d);
      // A computed number comes as {total, parts}: show the total, explain it on hover and click.
      const n = v && typeof v === "object" ? v.total : v;
      el.textContent = n === undefined ? "—" : el.dataset.fmt === "sign" ? sign(n) : n;
      el.title = v?.parts ? explain(v) : "";
      el.classList.toggle("explained", !!v?.parts);
      // Shown only when something changes it (an item raising or setting the score).
      if (el.dataset.unlessBase) el.hidden = !v?.parts || (v.parts.length === 1 && v.parts[0].label === "Base");
    }
    this.el.classList.toggle("no-spellcasting", this.derived.spell_save_dc === undefined);
    const bar = this.sections.hpbar;  // max HP is derived (CON, level), so the bar follows it
    bar?.body.replaceChildren(...[bar.build()].flat());
  }

  // -- building blocks -------------------------------------------------------

  field(path, { type = "text", label, placeholder, cls, min, max, rows, reason, after } = {}) {
    const v = getPath(this.sheet, path);
    let el;
    if (type === "textarea") el = h("textarea", { value: v ?? "", rows: rows || 4, placeholder });
    else if (type === "checkbox") el = h("input", { type, checked: !!v });
    else el = h("input", { type, value: v ?? "", placeholder, min, max, step: type === "number" ? 1 : undefined });
    if (cls) el.classList.add(...cls.split(" "));
    el.setAttribute("aria-label", label || placeholder || path);
    el.addEventListener("change", () => {
      const val = type === "checkbox" ? el.checked : type === "number" ? Math.trunc(Number(el.value) || 0) : el.value;
      setPath(this.sheet, path, val);
      this.changed(reason || `edited ${humanPath(path)}`);
      after?.();
    });
    return label ? h("label.field", h("span", label), el) : el;
  }

  derivedSpan(path, fmt = "sign", cls = "") {
    return h(`span.derived${cls ? "." + cls : ""}`, {
      dataset: { d: path, fmt },
      onclick: (e) => showParts(e.currentTarget, getPath(this.derived, path)),
    });
  }

  section(key, title, build, cls = "") {
    const body = h("div.card-body");
    this.sections[key] = { body, build };
    body.replaceChildren(...[build()].flat());
    return h(`section.card${cls ? "." + cls : ""}`, title && h("h2", title), body);
  }

  refresh(key) {
    const s = this.sections[key];
    if (!s) return;
    s.body.replaceChildren(...[s.build()].flat());
    this.updateDerived();
  }

  // A table of editable rows with add/remove.
  rows(key, path, cols, blank, noun) {
    const items = getPath(this.sheet, path);
    const template = cols.map((c) => c.w || "1fr").join(" ") + " auto";
    return h("div.rows",
      items.length === 0 && h("p.empty", `No ${noun}s.`),
      items.map((_, i) => h("div.row", { style: `grid-template-columns:${template}` },
        // A column with no key edits the item itself (lists of plain strings).
        cols.map((c) => this.field(c.key ? `${path}.${i}.${c.key}` : `${path}.${i}`, { type: c.type, placeholder: c.label || noun, reason: `edited ${noun}` })),
        h("button.icon", {
          title: `Remove ${noun}`, "aria-label": `Remove ${noun}`,
          onclick: () => { items.splice(i, 1); this.changed(`removed ${noun}`); this.refresh(key); },
        }, "×"),
      )),
      h("button.add", {
        onclick: () => {
          items.push(structuredClone(blank));
          this.refresh(key);
          this.sections[key].body.querySelector(".row:last-of-type input")?.focus();
          this.changed(`added ${noun}`);
        },
      }, `+ Add ${noun}`),
    );
  }

  // Free-text tags (proficiencies, conditions) as removable chips.
  chips(key, path, { suggestions = [], placeholder = "Add…" } = {}) {
    const items = getPath(this.sheet, path);
    const add = (v) => {
      v = v.trim();
      if (!v || items.includes(v)) return;
      items.push(v);
      this.changed(`added ${v}`);
      this.refresh(key);
      this.sections[key].body.querySelector("input")?.focus();
    };
    const listId = `dl-${key}`;
    const input = h("input", {
      placeholder, list: suggestions.length ? listId : undefined, "aria-label": placeholder,
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); add(input.value); } },
      onchange: () => { if (suggestions.includes(input.value)) add(input.value); },
    });
    if (suggestions.length) input.setAttribute("list", listId);
    return h("div.chips",
      items.map((v, i) => h("span.chip", v,
        h("button", {
          title: `Remove ${v}`, "aria-label": `Remove ${v}`,
          onclick: () => { items.splice(i, 1); this.changed(`removed ${v}`); this.refresh(key); },
        }, "×"))),
      input,
      suggestions.length > 0 && h("datalist", { id: listId }, suggestions.filter((s) => !items.includes(s)).map((s) => h("option", { value: s }))),
    );
  }

  // -- layout ----------------------------------------------------------------

  render() {
    this.status = h("span.save-status", "Saved");
    return h("div.sheet",
      h("div.sheet-head",
        this.field("name", { cls: "name-input", reason: "renamed", placeholder: "Name" }),
        this.status,
        h("div.head-actions",
          h("button.primary.keep-enabled", { onclick: () => this.play() }, "Play view"),
          h("button.keep-enabled", { onclick: () => this.exportJson() }, "Export"),
          this.canEdit && h("button", { onclick: () => this.copyTo() }, "Copy to campaign…"),
          this.canEdit && h("button.danger", { onclick: () => this.remove() }, "Delete"),
        ),
      ),
      h("p.muted.small",
        "In ", h("a", { href: `#/campaigns/${this.campaign.slug}/party` }, this.campaign.name),
        " · ", h("a", { href: `#/campaigns/${this.campaign.slug}/entity/${this.entityId}` }, "world entry"),
        this.copiedFrom && [" · copied from ", this.copiedFrom.id
          ? h("a", { href: `#/characters/${this.copiedFrom.id}` }, `${this.copiedFrom.name} (${this.copiedFrom.campaign.name})`)
          : `${this.copiedFrom.name} (${this.copiedFrom.campaign.name})`]),
      this.filesLine(),
      h("div.identity",
        this.field("species", { label: "Species" }),
        this.field("background", { label: "Background" }),
        this.field("alignment", { label: "Alignment" }),
        this.field("player", { label: "Player" }),
        h("label.field", h("span", "Nicknames"), h("input", {
          value: (this.sheet.nicknames || []).join(", "), placeholder: "Other names, comma-separated", "aria-label": "Nicknames",
          onchange: (e) => {
            this.sheet.nicknames = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
            this.changed("nicknames");
          } })),
        this.field("xp", { label: "XP", type: "number", min: 0 }),
      ),
      this.section("classes", "Classes", () => [
        this.rows("classes", "classes",
          [{ key: "name", label: "Class" }, { key: "subclass", label: "Subclass" }, { key: "level", label: "Lvl", type: "number", w: "4.5rem" }],
          { name: "", level: 1, subclass: "" }, "class"),
        h("p.muted", "Level ", this.derivedSpan("level", "plain"), " · Proficiency ", this.derivedSpan("proficiency_bonus")),
      ]),
      this.vitals(),
      h("div.sheet-grid",
        h("div.col",
          this.section("defense", "Defense & hit points", () => this.defense()),
          this.section("abilities", "Abilities", () => this.abilities()),
          this.section("saves", "Saving throws", () => this.saves()),
          this.section("skills", "Skills", () => this.skills()),
        ),
        h("div.col",
          this.section("resources", "Resources", () => this.resources()),
          this.section("attacks", "Attacks", () => this.attacks()),
          this.section("attack_options", "Attack options", () => this.attackOptions()),
          this.section("statuses", "Statuses", () => this.statuses()),
          this.section("bonuses", "Bonuses & effects", () => [
            h("p.muted.small", "Features and effects that add to a number. One that only counts during a status "
              + "(Rage's damage) names it. Items' bonuses are on the items."),
            this.bonuses("bonuses", "bonuses"),
          ]),
          this.section("spells", "Spells", () => this.spells(), "spells-card"),
        ),
        h("div.col",
          this.section("inventory", "Inventory", () => [
            h("div.coins", COINS.map((c) => this.field(`currency.${c}`, { type: "number", label: c.toUpperCase(), min: 0, reason: "coin" }))),
            this.inventory(),
          ]),
          this.section("features", "Features & traits", () => this.rows("features", "features",
            [{ key: null, label: "Feature or trait" }], "", "feature")),
          this.section("proficiencies", "Proficiencies & languages", () => this.chips("proficiencies", "proficiencies", { placeholder: "Add proficiency…" })),
          this.section("notes", "Notes", () => [
            this.field("personality", { type: "textarea", label: "Personality", rows: 3 }),
            this.field("notes", { type: "textarea", label: "Notes", rows: 6 }),
          ]),
          this.section("world", "In the world", () => (this.worldBox = h("div", h("p.muted", "Loading…")))),
          this.section("log", "History", () => h("ul.log", this.log.map((l) => h("li", h("time", l.created_at.replace("T", " ").slice(0, 16)), " ", l.reason)))),
        ),
      ),
    );
  }

  vitals() {
    const amount = h("input.hp-amount", { type: "number", min: 0, placeholder: "Amount", "aria-label": "HP amount" });
    const apply = (dir) => {
      const n = Math.trunc(Number(amount.value));
      if (!n) return amount.focus();
      this.action("POST", `/api/characters/${this.id}/hp`, { delta: dir * n });
    };
    amount.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(-1); });
    const hp = this.sheet.hp;
    return h("section.card.vitals",
      h("div.hp-block",
        h("div.hp-numbers",
          this.field("hp.current", { type: "number", cls: "hp-current", label: "HP", reason: "hp", after: () => this.refresh("hpbar") }),
          h("span.slash", "/"),
          h("label.field", h("span", "Max"), this.derivedSpan("hp_max", "plain", "hp-max")),
          this.field("hp.temp", { type: "number", min: 0, label: "Temp", reason: "temp hp" }),
        ),
        this.section("hpbar", null, () => hpBar({ ...hp, max: this.derived.hp_max?.total ?? 0 }), "bare"),
        h("div.hp-controls", amount,
          h("button.danger", { onclick: () => apply(-1) }, "Damage"),
          h("button.ok", { onclick: () => apply(1) }, "Heal"),
        ),
      ),
      h("div.stats",
        stat("AC", this.derivedSpan("ac", "plain")),
        stat("Speed", this.derivedSpan("speed", "plain")),
        stat("Initiative", this.derivedSpan("initiative")),
        stat("Prof.", this.derivedSpan("proficiency_bonus")),
        stat("Passive Perc.", this.derivedSpan("passive_perception", "plain")),
        stat("Spell DC", this.derivedSpan("spell_save_dc", "plain"), "spell-only"),
        stat("Spell atk", this.derivedSpan("spell_attack"), "spell-only"),
      ),
      h("div.vitals-row",
        this.section("death", null, () => this.deathSaves(), "bare"),
        h("div.rest-buttons",
          h("button", { onclick: () => this.action("POST", `/api/characters/${this.id}/rest`, { long: false }) }, "Short rest"),
          h("button", { onclick: () => this.action("POST", `/api/characters/${this.id}/rest`, { long: true }) }, "Long rest"),
        ),
      ),
      h("div.conditions", h("span.label", "Conditions"),
        this.section("conditions", null, () => this.chips("conditions", "conditions", { suggestions: CONDITIONS, placeholder: "Add condition…" }), "bare")),
    );
  }

  // AC's formula, base speed, and the hit-dice part of max HP.
  defense() {
    const ac = this.sheet.ac;
    const maxDex = h("input", {
      type: "number", min: 0, value: ac.max_dex ?? "", placeholder: "none", "aria-label": "Highest DEX bonus",
      onchange: () => { ac.max_dex = maxDex.value === "" ? null : Math.trunc(Number(maxDex.value)); this.changed("AC"); },
    });
    return [
      h("div.identity.compact",
        this.field("ac.label", { label: "Armor", placeholder: "Unarmored", reason: "AC" }),
        this.field("ac.base", { type: "number", label: "Base AC", reason: "AC" }),
        h("label.field", h("span", "Max DEX"), maxDex),
      ),
      h("div.ac-abilities", h("span.muted.small", "Adds"),
        ABILITIES.map((a) => h("label.mini",
          h("input", { type: "checkbox", checked: ac.abilities.includes(a),
            onchange: (e) => this.toggleIn("ac.abilities", a, e.target.checked, "AC") }),
          a.toUpperCase()))),
      h("p.parts-line", "AC ", this.derivedSpan("ac", "plain"), " · ", h("span.muted.small", "shields and magic armor are item bonuses")),
      h("div.identity.compact",
        this.field("speed", { type: "number", label: "Base speed", reason: "speed" }),
        this.field("hp.base", { type: "number", label: "HP from hit dice", reason: "max hp" }),
      ),
      h("p.parts-line", "Max HP ", this.derivedSpan("hp_max", "plain"), h("span.muted.small", " · Constitution is added per level")),
    ];
  }

  attacks() {
    const items = this.sheet.attacks;
    const remove = (list, i, noun, key) => h("button.icon", { title: `Remove ${noun}`, "aria-label": `Remove ${noun}`,
      onclick: () => { list.splice(i, 1); this.changed(`removed ${noun}`); this.refresh(key); } }, "×");
    return h("div.rows",
      items.length === 0 && h("p.empty", "No attacks."),
      items.map((a, i) => h("div.attack",
        h("div.attack-top",
          this.field(`attacks.${i}.name`, { placeholder: "Attack", reason: "attack" }),
          h("span.attack-total", this.derivedSpan(`attacks.${i}.to_hit`), " · ", this.derivedSpan(`attacks.${i}.text`, "plain")),
          remove(items, i, "attack", "attacks"),
        ),
        h("div.attack-parts",
          h("select", { "aria-label": "Ability", value: a.ability,
            onchange: (e) => { a.ability = e.target.value; this.changed("attack ability"); } },
            Object.entries(ATTACK_ABILITIES).map(([v, l]) => h("option", { value: v }, l))),
          h("label.mini", h("input", { type: "checkbox", checked: a.proficient,
            onchange: (e) => { a.proficient = e.target.checked; this.changed("attack proficiency"); } }), "Prof"),
          h("label.mini", "Magic +", this.field(`attacks.${i}.bonus`, { type: "number", reason: "attack bonus" })),
          h("label.mini", "Hit +", this.field(`attacks.${i}.hit_bonus`, { type: "number", reason: "attack bonus" })),
          this.field(`attacks.${i}.range`, { placeholder: "Range", reason: "attack range" }),
        ),
        a.damage.map((d, j) => h("div.damage-part",
          this.field(`attacks.${i}.damage.${j}.dice`, { placeholder: "1d8", reason: "damage" }),
          h("label.mini", h("input", { type: "checkbox", checked: d.add_ability, title: "Adds the attack's ability modifier",
            onchange: (e) => { d.add_ability = e.target.checked; this.changed("damage"); } }), "+mod"),
          h("label.mini", "+", this.field(`attacks.${i}.damage.${j}.bonus`, { type: "number", reason: "damage" })),
          this.field(`attacks.${i}.damage.${j}.type`, { placeholder: "type", reason: "damage" }),
          this.field(`attacks.${i}.damage.${j}.situational`, { placeholder: "only when… (situational)", reason: "damage", cls: "situational" }),
          d.situational && h("select", { "aria-label": "Spends", title: "Resource used up when this damage is added", value: d.spends || "",
            onchange: (e) => { d.spends = e.target.value; this.changed("damage"); } },
            h("option", { value: "" }, "spends nothing"),
            [...new Set([...this.sheet.resources.map((r) => r.name), d.spends].filter(Boolean))]
              .map((name) => h("option", { value: name }, `spends ${name}`))),
          d.situational && h("label.mini", { title: "Offered once a turn in combat (Sneak Attack, Charge)" },
            h("input", { type: "checkbox", checked: !!d.once_per_turn,
              onchange: (e) => { d.once_per_turn = e.target.checked; this.changed("damage"); } }), "1/turn"),
          remove(a.damage, j, "damage", "attacks"),
        )),
        this.chips("attacks", `attacks.${i}.properties`, { placeholder: "Add property (finesse, reach…)" }),
        h("div.attack-bottom",
          h("button.add", { onclick: () => {
            a.damage.push({ dice: "", type: "", add_ability: a.damage.length === 0, bonus: 0 });
            this.changed("added damage"); this.refresh("attacks");
          } }, "+ damage"),
          this.field(`attacks.${i}.notes`, { placeholder: "Notes (what else it does)", reason: "attack notes" }),
        ),
      )),
      h("button.add", { onclick: () => {
        items.push({ name: "", ability: "str", proficient: true, bonus: 0, hit_bonus: 0, range: "", properties: [], notes: "",
          damage: [{ dice: "", type: "", add_ability: true, bonus: 0 }] });
        this.refresh("attacks");
        this.sections.attacks.body.querySelector(".attack:last-of-type input")?.focus();
        this.changed("added attack");
      } }, "+ Add attack"),
    );
  }

  // Each item opens to show attunement, notes and the bonuses it gives while equipped.
  inventory() {
    const items = this.sheet.inventory;
    this.openItems ??= new Set();
    return h("div.rows",
      items.length === 0 && h("p.empty", "No items."),
      items.map((it, i) => {
        const open = this.openItems.has(i);
        const tags = [it.bonuses?.length ? `${it.bonuses.length} bonus${it.bonuses.length > 1 ? "es" : ""}` : "",
          it.requires_attunement ? (it.attuned ? "attuned" : "needs attunement") : ""].filter(Boolean).join(" · ");
        return h("div.item",
          h("div.row", { style: "grid-template-columns:1fr 4rem auto auto auto" },
            this.field(`inventory.${i}.name`, { placeholder: "Item", reason: "item" }),
            this.field(`inventory.${i}.qty`, { type: "number", placeholder: "Qty", reason: "item" }),
            h("label.mini", { title: "Equipped" }, h("input", { type: "checkbox", checked: it.equipped,
              onchange: (e) => { it.equipped = e.target.checked; this.changed(`${it.name || "item"} ${it.equipped ? "equipped" : "unequipped"}`); } }), "E"),
            h("button.icon", { title: open ? "Less" : "Attunement, notes and bonuses", "aria-label": `Details of ${it.name}`, "aria-expanded": String(open),
              onclick: () => { open ? this.openItems.delete(i) : this.openItems.add(i); this.refresh("inventory"); } }, open ? "▾" : "▸"),
            h("button.icon", { title: "Remove item", "aria-label": "Remove item",
              onclick: () => { items.splice(i, 1); this.openItems.clear(); this.changed("removed item"); this.refresh("inventory"); } }, "×"),
          ),
          !open && tags && h("p.muted.small.item-tags", tags),
          open && h("div.item-details",
            h("div.item-flags",
              h("label.mini", h("input", { type: "checkbox", checked: it.requires_attunement,
                onchange: (e) => { it.requires_attunement = e.target.checked; this.changed("attunement"); } }), "Needs attunement"),
              h("label.mini", h("input", { type: "checkbox", checked: it.attuned,
                onchange: (e) => { it.attuned = e.target.checked; this.changed(`${it.name || "item"} ${it.attuned ? "attuned" : "unattuned"}`); } }), "Attuned"),
            ),
            this.field(`inventory.${i}.notes`, { type: "textarea", rows: 2, placeholder: "What it does", reason: "item notes" }),
            h("span.muted.small", "Bonuses while equipped"),
            this.bonuses("inventory", `inventory.${i}.bonuses`),
          ),
        );
      }),
      h("button.add", { onclick: () => {
        items.push({ name: "", qty: 1, equipped: false, attuned: false, requires_attunement: false, bonuses: [], notes: "" });
        this.refresh("inventory");
        this.sections.inventory.body.querySelector(".item:last-of-type input")?.focus();
        this.changed("added item");
      } }, "+ Add item"),
    );
  }

  // Bonuses at `path` (the sheet's, or an item's), shown in section `key`.
  bonuses(key, path) {
    const list = getPath(this.sheet, path);
    return h("div.rows.bonuses",
      list.map((b, i) => h("div.row.bonus", { style: "grid-template-columns:auto 1fr 1fr 5rem 7rem auto" },
        h("input", { type: "checkbox", checked: b.active, title: "On", "aria-label": `${b.source || "bonus"} on`,
          onchange: (e) => { b.active = e.target.checked; this.changed(`${b.source || "bonus"} ${b.active ? "on" : "off"}`); } }),
        this.field(`${path}.${i}.source`, { placeholder: "Source", reason: "bonus" }),
        h("select", { "aria-label": "Applies to", value: b.target,
          onchange: (e) => { b.target = e.target.value; this.changed("bonus"); } },
          BONUS_TARGETS.map(([group, opts]) => h("optgroup", { label: group },
            opts.map(([v, l]) => h("option", { value: v }, l))))),
        h("input", { value: b.amount, placeholder: "+1, prof, level…", "aria-label": "Amount",
          title: "A number, or prof, half_prof, level, an ability (cha), or e.g. 2*level",
          onchange: (e) => {
            const v = e.target.value.trim();
            b.amount = /^[+-]?\d+$/.test(v) ? Number(v) : v;
            this.changed("bonus");
          } }),
        h("select", { "aria-label": "During", title: "Counts only while this status is on", value: b.during || "",
          onchange: (e) => { b.during = e.target.value; this.changed("bonus"); } },
          h("option", { value: "" }, "always"),
          [...new Set([...this.sheet.statuses.map((st) => st.name), b.during].filter(Boolean))]
            .map((name) => h("option", { value: name }, `during ${name}`))),
        h("button.icon", { title: "Remove bonus", "aria-label": "Remove bonus",
          onclick: () => { list.splice(i, 1); this.changed("removed bonus"); this.refresh(key); } }, "×"),
      )),
      h("button.add", { onclick: () => {
        list.push({ target: "ac", amount: 1, source: "", active: true, during: "" });
        this.changed("added bonus"); this.refresh(key);
      } }, "+ Add bonus"),
    );
  }

  // Things switched on and off in play. A bonus can count only during one; switching one on can spend a resource.
  statuses() {
    const list = this.sheet.statuses;
    return h("div.rows",
      list.length === 0 && h("p.empty", "Rage, Concentration, Bless… switched on in play."),
      list.length > 0 && h("p.muted.small", "Rounds: how long it lasts in combat (Rage, Bless: 10); blank lasts until ended."),
      list.map((st, i) => h("div.row.status-row", { style: "grid-template-columns:auto 1fr 1fr 8rem 4.5rem auto" },
        h("input", { type: "checkbox", checked: st.active, title: "On now", "aria-label": `${st.name || "status"} on`,
          onchange: (e) => { st.active = e.target.checked; this.changed(`${st.name || "status"} ${st.active ? "on" : "off"}`); } }),
        this.field(`statuses.${i}.name`, { placeholder: "Status", reason: "status", after: () => this.refresh("bonuses") }),
        this.field(`statuses.${i}.note`, { placeholder: "On what (the spell…)", reason: "status" }),
        h("select", { "aria-label": "Spends", title: "Resource spent when it's switched on", value: st.resource || "",
          onchange: (e) => { st.resource = e.target.value; this.changed("status"); } },
          h("option", { value: "" }, "spends nothing"),
          [...new Set([...this.sheet.resources.map((r) => r.name), st.resource].filter(Boolean))]
            .map((name) => h("option", { value: name }, `spends ${name}`))),
        this.field(`statuses.${i}.duration`, { type: "number", min: 0, placeholder: "rounds", label: undefined, reason: "status duration",
          cls: "status-rounds" }),
        h("button.icon", { title: "Remove status", "aria-label": "Remove status",
          onclick: () => { list.splice(i, 1); this.changed("removed status"); this.refresh("statuses"); this.refresh("bonuses"); } }, "×"),
      )),
      h("button.add", { onclick: () => {
        list.push({ name: "", active: false, note: "", resource: "" });
        this.refresh("statuses");
        this.sections.statuses.body.querySelector(".status-row:last-of-type input:not([type=checkbox])")?.focus();
        this.changed("added status");
      } }, "+ Add status"),
    );
  }

  // Choices made before an attack roll. The Play view offers each on the attacks it applies to.
  attackOptions() {
    const list = this.sheet.attack_options;
    const add = (preset) => {
      list.push({ name: "", applies_to: [], advantage: "", to_hit: 0, damage: 0, note: "", ...preset });
      this.refresh("attack_options");
      if (!preset.name) this.sections.attack_options.body.querySelector(".option:last-of-type input")?.focus();
      this.changed(`added ${preset.name || "attack option"}`);
    };
    const presets = h("select", { "aria-label": "Add an attack option", onchange: (e) => {
      const p = ATTACK_OPTION_PRESETS[e.target.value];
      e.target.value = "";
      if (p) add(p);
    } }, h("option", { value: "" }, "+ Add an attack option…"),
      Object.keys(ATTACK_OPTION_PRESETS).map((k) => h("option", { value: k }, k)));
    return h("div.rows",
      list.length === 0 && h("p.empty", "Reckless Attack, Great Weapon Master… chosen when attacking."),
      list.map((o, i) => h("div.option",
        h("div.option-top",
          this.field(`attack_options.${i}.name`, { placeholder: "Option", reason: "attack option" }),
          h("select", { "aria-label": "Advantage", value: o.advantage,
            onchange: (e) => { o.advantage = e.target.value; this.changed("attack option"); } },
            h("option", { value: "" }, "no advantage"), h("option", { value: "advantage" }, "advantage"),
            h("option", { value: "disadvantage" }, "disadvantage")),
          h("button.icon", { title: "Remove", "aria-label": "Remove attack option",
            onclick: () => { list.splice(i, 1); this.changed("removed attack option"); this.refresh("attack_options"); } }, "×"),
        ),
        h("div.attack-parts",
          h("label.mini", "Hit", this.field(`attack_options.${i}.to_hit`, { reason: "attack option", cls: "amount" })),
          h("label.mini", "Damage", this.field(`attack_options.${i}.damage`, { reason: "attack option", cls: "amount" })),
        ),
        this.field(`attack_options.${i}.note`, { placeholder: "Reminder when chosen (what it costs)", reason: "attack option" }),
        h("div.option-applies", h("span.muted.small", "Applies to"),
          this.chips("attack_options", `attack_options.${i}.applies_to`, {
            suggestions: ["melee", "ranged", "weapon", "spell", ...ABILITIES, "heavy", "finesse", "light", "thrown"],
            placeholder: "every attack; or str, melee, heavy…",
          })),
      )),
      presets,
    );
  }

  deathSaves() {
    const box = (kind) => h("div.death",
      h("span.label", kind === "successes" ? "Death saves ✓" : "✗"),
      [0, 1, 2].map((i) => h("input", {
        type: "checkbox", checked: i < this.sheet.death_saves[kind], "aria-label": `${kind} ${i + 1}`,
        onchange: (e) => {
          this.sheet.death_saves[kind] = e.target.checked ? i + 1 : i;
          this.changed("death saves");
          this.refresh("death");
        },
      })));
    return h("div.death-saves", box("successes"), box("failures"));
  }

  abilities() {
    return h("div.abilities", ABILITIES.map((a) => h("div.ability",
      h("span.ability-name", { title: ABILITY_NAMES[a] }, a.toUpperCase()),
      this.derivedSpan(`modifiers.${a}`, "sign", "ability-mod"),
      this.field(`abilities.${a}`, { type: "number", min: 1, max: 30, cls: "ability-score", label: undefined, reason: `${a.toUpperCase()} score` }),
      unlessBase(this.derivedSpan(`scores.${a}`, "plain", "ability-effective")),
    )));
  }

  toggleIn(listPath, value, on, reason) {
    const list = getPath(this.sheet, listPath);
    const i = list.indexOf(value);
    if (on && i < 0) list.push(value);
    if (!on && i >= 0) list.splice(i, 1);
    this.changed(reason);
  }

  saves() {
    return h("ul.checklist", ABILITIES.map((a) => h("li",
      h("label",
        h("input", { type: "checkbox", checked: this.sheet.save_proficiencies.includes(a),
          onchange: (e) => this.toggleIn("save_proficiencies", a, e.target.checked, `${a} save proficiency`) }),
        ABILITY_NAMES[a]),
      this.derivedSpan(`saves.${a}`),
    )));
  }

  skills() {
    return h("ul.checklist.skills",
      h("li.legend", h("span", "Prof · Exp"), h("span")),
      Object.entries(SKILLS).map(([skill, ab]) => h("li",
        h("span.skill-boxes",
          h("input", { type: "checkbox", title: "Proficient", "aria-label": `${skill} proficient`, checked: this.sheet.skill_proficiencies.includes(skill),
            onchange: (e) => this.toggleIn("skill_proficiencies", skill, e.target.checked, `${skill} proficiency`) }),
          h("input", { type: "checkbox", title: "Expertise", "aria-label": `${skill} expertise`, checked: this.sheet.skill_expertise.includes(skill),
            onchange: (e) => this.toggleIn("skill_expertise", skill, e.target.checked, `${skill} expertise`) }),
        ),
        h("span.skill-name", skill, h("small", ` (${ab})`)),
        this.derivedSpan(`skills.${skill}`),
      )));
  }

  resources() {
    const items = this.sheet.resources;
    return h("div.rows",
      items.length === 0 && h("p.empty", "Spell slots, hit dice, ki, rages, item charges…"),
      items.map((r, i) => {
        const setUsed = (used) => {
          r.used = Math.max(0, Math.min(r.max, used));
          this.changed(`${r.name || "resource"} ${r.max - r.used}/${r.max}`);
          this.refresh("resources");
        };
        // Pips show what's left: filled = available. Clicking a pip spends or restores up to it.
        const pips = r.max <= 12
          ? h("div.pips", Array.from({ length: r.max }, (_, p) => h("button.pip", {
              className: `pip${p < r.max - r.used ? " full" : ""}`, title: `${r.max - r.used}/${r.max} left`,
              "aria-label": `${r.name} ${p + 1}`,
              onclick: () => setUsed(p < r.max - r.used ? r.max - p : r.max - p - 1),
            })))
          : h("div.counter",
              h("button.icon", { onclick: () => setUsed(r.used + 1), "aria-label": "Spend one" }, "−"),
              h("span", `${r.max - r.used}/${r.max}`),
              h("button.icon", { onclick: () => setUsed(r.used - 1), "aria-label": "Restore one" }, "+"));
        return h("div.resource",
          h("div.resource-top",
            this.field(`resources.${i}.name`, { placeholder: "Name", reason: "resource" }),
            h("button.icon", { title: "Remove", "aria-label": "Remove resource",
              onclick: () => { items.splice(i, 1); this.changed("removed resource"); this.refresh("resources"); } }, "×"),
          ),
          h("div.resource-bottom",
            pips,
            h("label.mini", "max", this.field(`resources.${i}.max`, { type: "number", min: 0, reason: "resource max", after: () => { r.used = Math.min(r.used, r.max); this.refresh("resources"); } })),
            h("select", {
              "aria-label": "Recharge", value: r.recharge,
              onchange: (e) => { r.recharge = e.target.value; this.changed("resource recharge"); },
            }, RECHARGE.map((v) => h("option", { value: v }, v || "manual"))),
          ),
        );
      }),
      h("button.add", { onclick: () => { items.push({ name: "", max: 1, used: 0, recharge: "long rest" }); this.refresh("resources"); this.changed("added resource"); } }, "+ Add resource"),
    );
  }

  spells() {
    const items = this.sheet.spells;
    const order = items.map((s, i) => [s, i]).sort((a, b) => a[0].level - b[0].level || a[0].name.localeCompare(b[0].name));
    const out = [h("div.identity.compact",
      h("label.field", h("span", "Casting ability"),
        h("select", {
          value: this.sheet.spellcasting_ability,
          onchange: (e) => { this.sheet.spellcasting_ability = e.target.value; this.changed("spellcasting ability"); },
        }, h("option", { value: "" }, "none"), ABILITIES.map((a) => h("option", { value: a }, ABILITY_NAMES[a])))),
    )];
    let level = -1;
    for (const [s, i] of order) {
      if (s.level !== level) {
        level = s.level;
        out.push(h("h3", level === 0 ? "Cantrips" : `Level ${level}`));
      }
      out.push(h("div.row.spell",
        h("input", { type: "checkbox", checked: s.prepared, title: "Prepared", "aria-label": `${s.name} prepared`,
          onchange: (e) => { s.prepared = e.target.checked; this.changed(`${s.name} prepared`); } }),
        this.field(`spells.${i}.name`, { placeholder: "Spell", reason: "spell" }),
        h("select", {
          "aria-label": "Spell level", value: String(s.level),
          onchange: (e) => { s.level = Number(e.target.value); this.changed("spell level"); this.refresh("spells"); },
        }, Array.from({ length: 10 }, (_, l) => h("option", { value: String(l) }, l === 0 ? "C" : l))),
        h("button.icon", { title: "Remove", "aria-label": `Remove ${s.name}`,
          onclick: () => { items.splice(i, 1); this.changed("removed spell"); this.refresh("spells"); } }, "×"),
      ));
    }
    out.push(h("button.add", {
      onclick: () => {
        items.push({ name: "", level: 0, prepared: true, notes: "" });
        this.refresh("spells");
        const inputs = this.sections.spells.body.querySelectorAll(".spell input:not([type=checkbox])");
        [...inputs].find((x) => !x.value)?.focus();
        this.changed("added spell");
      },
    }, "+ Add spell"));
    return out;
  }

  exportJson() {
    const blob = new Blob([JSON.stringify(this.sheet, null, 2)], { type: "application/json" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `${this.sheet.name}.json` });
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async remove() {
    if (!confirm(`Delete ${this.sheet.name}? This can't be undone.`)) return;
    await api("DELETE", `/api/characters/${this.id}`);
    location.hash = `#/campaigns/${this.campaign.slug}/party`;
  }
}

// A derived number shown only when something changes it from the sheet's own value.
function unlessBase(el) {
  el.dataset.unlessBase = "1";
  return el;
}

// "STR +4 · Proficiency +3" for a {total, parts} number.
function explain(v) {
  return v.parts.map((p) => `${p.label} ${p.label === "Base" || p.label === "Hit dice" ? p.value : sign(p.value)}`).join(" · ");
}

// A small popover with a number's parts, by the element clicked.
function showParts(el, v) {
  document.querySelector(".parts-pop")?.remove();
  if (!v?.parts) return;
  const pop = h("div.parts-pop", { role: "tooltip" },
    v.parts.map((p) => h("div", h("span", p.label), h("b", p.label === "Base" || p.label === "Hit dice" ? p.value : sign(p.value)))),
    h("div.parts-total", h("span", "Total"), h("b", el.textContent)));
  document.body.append(pop);
  const r = el.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(window.scrollX + r.left, window.scrollX + innerWidth - pop.offsetWidth - 8))}px`;
  pop.style.top = `${window.scrollY + r.bottom + 4}px`;
  setTimeout(() => document.addEventListener("click", () => pop.remove(), { once: true }));
}

function stat(label, valueEl, cls = "") {
  return h(`div.stat${cls ? "." + cls : ""}`, h("span.stat-label", label), valueEl);
}

function humanPath(path) {
  return path.replace(/\.\d+\./g, " ").replace(/\./g, " ");
}
