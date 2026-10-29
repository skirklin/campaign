// Character list and the editable character sheet.
//
// The sheet is built once and edited in place: every input writes into
// `this.sheet` and schedules a debounced save. The server validates and
// returns the derived numbers (modifiers, skills, DCs), which are patched
// into elements marked `data-d` so typing never loses focus. Only list
// sections re-render, when rows are added or removed.

import { api, h, toast, sign, getPath, setPath, debounce } from "./util.js";

const ABILITIES = ["str", "dex", "con", "int", "wis", "cha"];
const ABILITY_NAMES = { str: "Strength", dex: "Dexterity", con: "Constitution", int: "Intelligence", wis: "Wisdom", cha: "Charisma" };
const SKILLS = {
  acrobatics: "dex", "animal handling": "wis", arcana: "int", athletics: "str", deception: "cha",
  history: "int", insight: "wis", intimidation: "cha", investigation: "int", medicine: "wis",
  nature: "int", perception: "wis", performance: "cha", persuasion: "cha", religion: "int",
  "sleight of hand": "dex", stealth: "dex", survival: "wis",
};
const CONDITIONS = [
  "blinded", "charmed", "deafened", "exhaustion", "frightened", "grappled", "incapacitated", "invisible",
  "paralyzed", "petrified", "poisoned", "prone", "restrained", "stunned", "unconscious", "concentrating",
];
const RECHARGE = ["", "short rest", "long rest", "dawn"];
const COINS = ["cp", "sp", "ep", "gp", "pp"];

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
  const data = await api("GET", `/api/characters/${id}`);
  return new SheetView(data).el;
}

class SheetView {
  constructor({ id, campaign, can_edit, entity_id, copied_from, sheet, derived, log }) {
    this.id = id;
    this.campaign = campaign;
    this.entityId = entity_id;
    this.copiedFrom = copied_from;
    this.canEdit = can_edit;
    this.sheet = sheet;
    this.derived = derived;
    this.log = log;
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
    const fresh = new SheetView(data);
    this.el.replaceWith(fresh.el);
  }

  updateDerived() {
    for (const el of this.el.querySelectorAll("[data-d]")) {
      const v = getPath(this.derived, el.dataset.d);
      el.textContent = v === undefined ? "—" : el.dataset.fmt === "sign" ? sign(v) : v;
    }
    this.el.classList.toggle("no-spellcasting", this.derived.spell_save_dc === undefined);
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
    return h(`span.derived${cls ? "." + cls : ""}`, { dataset: { d: path, fmt } });
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
      h("div.identity",
        this.field("species", { label: "Species" }),
        this.field("background", { label: "Background" }),
        this.field("alignment", { label: "Alignment" }),
        this.field("player", { label: "Player" }),
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
          this.section("abilities", "Abilities", () => this.abilities()),
          this.section("saves", "Saving throws", () => this.saves()),
          this.section("skills", "Skills", () => this.skills()),
        ),
        h("div.col",
          this.section("resources", "Resources", () => this.resources()),
          this.section("attacks", "Attacks", () => this.rows("attacks", "attacks",
            [{ key: "name", label: "Attack" }, { key: "to_hit", label: "Hit", w: "4rem" }, { key: "damage", label: "Damage" }],
            { name: "", to_hit: "", damage: "", notes: "" }, "attack")),
          this.section("spells", "Spells", () => this.spells(), "spells-card"),
        ),
        h("div.col",
          this.section("inventory", "Inventory", () => [
            h("div.coins", COINS.map((c) => this.field(`currency.${c}`, { type: "number", label: c.toUpperCase(), min: 0, reason: "coin" }))),
            this.rows("inventory", "inventory",
              [{ key: "name", label: "Item" }, { key: "qty", label: "Qty", type: "number", w: "4rem" }, { key: "equipped", label: "Equipped", type: "checkbox", w: "auto" }],
              { name: "", qty: 1, equipped: false, attuned: false, notes: "" }, "item"),
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
          this.field("hp.max", { type: "number", min: 0, label: "Max", reason: "max hp", after: () => this.refresh("hpbar") }),
          this.field("hp.temp", { type: "number", min: 0, label: "Temp", reason: "temp hp" }),
        ),
        this.section("hpbar", null, () => hpBar(hp), "bare"),
        h("div.hp-controls", amount,
          h("button.danger", { onclick: () => apply(-1) }, "Damage"),
          h("button.ok", { onclick: () => apply(1) }, "Heal"),
        ),
      ),
      h("div.stats",
        stat("AC", this.field("ac", { type: "number", cls: "stat-input", label: undefined })),
        stat("Speed", this.field("speed", { type: "number", cls: "stat-input" })),
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

function stat(label, valueEl, cls = "") {
  return h(`div.stat${cls ? "." + cls : ""}`, h("span.stat-label", label), valueEl);
}

function humanPath(path) {
  return path.replace(/\.\d+\./g, " ").replace(/\./g, " ");
}
