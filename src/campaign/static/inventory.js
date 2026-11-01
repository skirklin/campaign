// The party's inventory: everything the party owns, by who has it.
//
// One list. The party's own things (loot nobody has claimed, the shared
// purse) come first, then each character's, then anything left with someone
// else. Claiming, giving and putting back all hand a thing to someone. An item
// that does something points at its world item's definition (its bonuses,
// attunement, worth), shared by every copy; saying what it does is the
// world's business, so it needs editor access, and the DM can keep it from
// the players until they identify it.
//
// Shown whole on the Party tab, and for one character (their things, then
// the party's unclaimed loot) on their sheet and the Play view's Gear tab.
// The server decides who may change what; can_edit on each holder says.

import { api, h, toast, sign, bottomSheet, keypad, askQty, worth, sessionNames } from "./util.js";
import { COINS, BONUS_TARGETS } from "./sheet.js";

// Close a sheet, then open what comes next once it's gone, so the back button stays in step with them.
function after(dialog, next) {
  dialog.addEventListener("close", () => next(), { once: true });
  dialog.close();
}

const coinWorth = (purse) => COINS.reduce((sum, c) => sum + (purse[c] || 0) * { cp: 0.01, sp: 0.1, ep: 0.5, gp: 1, pp: 10 }[c], 0);

// "+1 AC, +1 saves" from an item's bonuses.
export function bonusText(bonuses) {
  return (bonuses || []).map((b) => `${typeof b.amount === "number" ? sign(b.amount) : `+${b.amount}`} ${targetLabel(b.target)}`
    + (b.during ? ` during ${b.during}` : "")).join(", ");
}

// "damage:str" -> "STR damage", "save" -> "saves", "ac" -> "AC".
export function targetLabel(t) {
  const [what, which] = t.split(":");
  const names = { ac: "AC", hp_max: "max HP", speed: "speed", initiative: "initiative", attack: "to hit", damage: "damage",
    save: "saves", skill: "skills", check: "checks", passive_perception: "passive Perception", spell_dc: "spell DC",
    spell_attack: "spell attack", score: "", score_set: "at least" };
  const base = names[what] ?? what;
  return which ? `${which.length <= 3 ? which.toUpperCase() : which} ${base}`.trim() : base;
}

export class InventoryView {
  // `focus`: a character's holder id: show their things, then the party's unclaimed loot, and take only for them.
  // `onChange()`: something changed (the focused character's sheet may count different gear now).
  constructor(slug, { focus = undefined, onChange = null } = {}) {
    this.slug = slug;
    this.base = `/api/campaigns/${slug}/inventory`;
    this.focus = focus;
    this.onChange = onChange;
    this.query = "";
    this.el = h("section.inventory", h("p.muted", "Loading the party's things…"));
    this.load();
  }

  async load() {
    try {
      this.sessionName = await sessionNames(this.slug);
      this.show(await api("GET", this.base));
    } catch (err) {
      this.el.replaceChildren(h("p.muted", err.message));
    }
  }

  show(data) {
    this.data = data;
    this.render();
  }

  async send(method, path, body) {
    try {
      this.show(await api(method, this.base + path, body));
      this.onChange?.();
      return true;
    } catch (err) {
      toast(err.message, true);
      this.load();
      return false;
    }
  }

  holder(id) {
    return this.data.holders.find((x) => x.id === id);
  }

  // Who this person can take things for: characters they may change (only the one in focus, if any).
  get takers() {
    const mine = this.data.holders.filter((x) => x.kind === "character" && x.can_edit);
    return this.focus === undefined ? mine : mine.filter((x) => x.id === this.focus);
  }

  // -- drawing -------------------------------------------------------------------

  render() {
    const focused = this.focus !== undefined;
    const holders = focused ? [this.holder(this.focus), this.holder(null)].filter(Boolean) : this.data.holders;
    const q = this.query.trim().toLowerCase();
    const party = this.holder(null);
    const blocks = holders.map((x) => this.holderBlock(x, q)).filter(Boolean);
    const search = !focused && h("input.inventory-search", {
      type: "search", value: this.query, placeholder: "Find an item…", "aria-label": "Find an item",
      oninput: (e) => {
        this.query = e.target.value;
        const at = e.target.selectionStart;
        this.render();
        const box = this.el.querySelector(".inventory-search");
        box.focus();
        box.setSelectionRange(at, at);
      },
    });
    const canShare = party && coinWorth(party.purse) > 0 && this.data.holders.some((x) => x.kind === "character");
    this.el.replaceChildren(...[
      !focused && h("div.block-head", h("h2", "Party inventory"),
        h("span.muted.small", this.data.worth_gp ? `worth ${worth(this.data.worth_gp)} in all` : "everything the party owns")),
      search,
      ...blocks,
      q && blocks.length === 0 && h("p.empty", `Nothing called “${this.query}”.`),
      !focused && canShare && h("button.add", { onclick: () => this.split() }, "Share the party's coin among the characters"),
      focused && h("p.small", h("a", { href: `#/campaigns/${this.slug}/party` }, "Everything the party has →")),
      !focused && this.data.log.length > 0 && h("details.inventory-log",
        h("summary.muted.small", "History"),
        h("ul.log", this.data.log.map((l) => h("li",
          h("span.session-tag", this.sessionName ? this.sessionName(l.session) : `session ${l.session}`), " ", l.text,
          l.who && h("span.muted", ` · ${l.who.split("@")[0]}`))))),
    ].filter(Boolean));
  }

  holderBlock(x, q) {
    const focused = this.focus !== undefined;
    const items = q ? x.items.filter((i) => `${i.name} ${i.notes} ${i.item?.name || ""}`.toLowerCase().includes(q)) : x.items;
    if (q && items.length === 0) return null;
    const title = x.kind === "party" ? (focused ? "Unclaimed" : "The party")
      : focused ? "Carried" : x.kind === "character" ? h("a", { href: `#/characters/${x.character_id}` }, x.name) : x.name;
    const note = x.kind === "party" ? (focused ? "the party's, for anyone to claim" : "unclaimed, and the shared purse")
      : x.kind === "other" ? "left with them" : "";
    const value = x.worth_gp ? `items worth ${worth(x.worth_gp)}` : "";
    const empty = x.kind === "party" ? "Nothing unclaimed. Loot the party takes goes here." : "Nothing.";
    return h(`div.play-block.holder.${x.kind}`,
      h("div.block-head", h("h2", title), h("span.muted.small", [note, value].filter(Boolean).join(" · "))),
      (x.kind !== "other" || COINS.some((c) => x.purse[c])) && h("div.play-coins", COINS.map((c) => h("button.play-coin", {
        disabled: !x.can_edit, onclick: () => this.coinSheet(x, c), "aria-label": `${x.name}: ${x.purse[c] || 0} ${c}`,
      }, h("strong", x.purse[c] || 0), h("span", c)))),
      items.length === 0 ? h("p.empty", q ? "" : empty) : h("div.play-list", items.map((i) => this.itemRow(i, x))),
      x.can_edit && !q && h("button.add", { onclick: () => this.addSheet(x) },
        x.kind === "party" ? "+ Add loot" : `+ Add to ${focused ? "what you carry" : `${x.name}'s things`}`),
    );
  }

  itemRow(i, x) {
    const details = [
      i.value_gp > 0 && `${worth(i.value_gp)}${i.qty !== 1 ? " each" : ""}`,
      bonusText(i.bonuses),
      i.requires_attunement && "needs attunement",
      i.notes,
    ].filter(Boolean).join(" · ");
    const claim = x.id === null && this.takers.length > 0;
    return h("div.play-row.item",
      h("button.link.row-main", { onclick: () => this.itemSheet(i, x), "aria-label": `${i.name}: more` },
        h("span", i.name, i.qty !== 1 && h("span.muted", ` ×${i.qty}`),
          i.item?.defined && h("span.magic", { title: "Does something: see what" }, " ✦"),
          i.item?.secret && h("span.muted", { title: "The party doesn't know what it does" }, " (secret)"),
          i.equipped && h("small.equipped", i.active ? " equipped" : " equipped, not attuned"),
          i.attuned && i.active && h("small.equipped", " · attuned")),
        details && h("small.muted", details)),
      claim && h("button", { onclick: () => this.claim(i) }, "Claim"));
  }

  // -- changes -------------------------------------------------------------------

  async claim(i) {
    const who = await this.pickTaker(`Claim ${i.name} for…`);
    if (!who) return;
    const n = await askQty(`Claim ${i.name}`, i.qty, `Claim for ${who.name}`);
    if (n) this.send("POST", `/items/${i.id}/move`, { to: who.id, qty: n });
  }

  pickTaker(title) {
    const takers = this.takers;
    if (takers.length <= 1) return Promise.resolve(takers[0] || null);
    return new Promise((resolve) => {
      let chosen = null;
      const dialog = bottomSheet(title, h("div.pick-list", takers.map((t) =>
        h("button.wide", { onclick: () => { chosen = t; dialog.close(); } }, t.name))));
      dialog.addEventListener("close", () => resolve(chosen));
    });
  }

  // Everyone something could go to from `x`: the party, the characters, whoever else holds things, or anyone by name.
  pickRecipient(title, x) {
    return new Promise((resolve) => {
      let chosen = null;
      const others = this.data.holders.filter((o) => o.id !== x.id);
      const name = h("input", { placeholder: "Someone else: an NPC's name", "aria-label": "Someone else" });
      const dialog = bottomSheet(title,
        h("div.pick-list", others.map((o) => h("button.wide", { onclick: () => { chosen = { to: o.id, name: o.name }; dialog.close(); } },
          o.kind === "party" ? "Back to the party" : o.name))),
        h("form.inline-form", {
          onsubmit: (e) => {
            e.preventDefault();
            if (!name.value.trim()) return name.focus();
            chosen = { to_name: name.value.trim(), name: name.value.trim() };
            dialog.close();
          },
        }, name, h("button", { type: "submit" }, "Give")));
      dialog.addEventListener("close", () => resolve(chosen));
    });
  }

  async give(i, x) {
    const to = await this.pickRecipient(`Give ${i.name} to…`, x);
    if (!to) return;
    const n = await askQty(`Give ${i.name} to ${to.name}`, i.qty, "Give");
    if (n) this.send("POST", `/items/${i.id}/move`, { to: to.to ?? null, to_name: to.to_name, qty: n });
  }

  async split() {
    if (!confirm("Share the party's coin evenly among the characters? What doesn't divide stays with the party.")) return;
    this.send("POST", "/coin/split");
  }

  coinSheet(x, coin) {
    let amount = 0;
    const go = (fn) => () => { if (amount) after(dialog, () => fn(amount)); };
    const change = (dir) => (n) => this.send("POST", "/coin", { holder: x.id, currency: { [coin]: dir * n } });
    const give = async (n) => {
      const to = await this.pickRecipient(`Give ${n} ${coin} to…`, x);
      if (to) this.send("POST", "/coin/give", { holder: x.id, to: to.to ?? null, to_name: to.to_name, currency: { [coin]: n } });
    };
    const takers = x.id === null ? this.takers : [];
    const dialog = bottomSheet(`${x.kind === "party" ? "The party's" : `${x.name}'s`} ${coin}: ${x.purse[coin] || 0}`,
      ...keypad((n) => (amount = n)),
      h("div.hp-buttons",
        h("button.danger-solid", { onclick: go(change(-1)) }, "Spend"),
        h("button.ok-solid", { onclick: go(change(1)) }, "Add")),
      h("div.hp-buttons.second",
        takers.length === 1 && h("button", {
          onclick: go((n) => this.send("POST", "/coin/give", { holder: null, to: takers[0].id, currency: { [coin]: n } })),
        }, `Take for ${takers[0].name}`),
        h("button", { onclick: go(give) }, "Give…")));
  }

  // An item: what it is and does, and what can be done with it.
  itemSheet(i, x) {
    const toggle = (key, label) => h("label.play-toggle",
      h("input", {
        type: "checkbox", checked: i[key], disabled: !x.can_edit,
        onchange: async (e) => { dialog.close(); this.send("PATCH", `/items/${i.id}`, { [key]: e.target.checked }); },
      }), label);
    const world = i.item?.id && h("a", { href: `#/campaigns/${this.slug}/entity/${i.item.id}`, onclick: () => dialog.close() },
      i.item.name === i.name ? "In the world" : `In the world: ${i.item.name}`);
    const bonuses = bonusText(i.bonuses);
    const takers = x.id === null ? this.takers : [];
    const dialog = bottomSheet(i.qty !== 1 ? `${i.name} ×${i.qty}` : i.name,
      i.notes ? h("p", i.notes) : null,
      (bonuses || i.requires_attunement) && h("p", [bonuses, i.requires_attunement && "needs attunement"].filter(Boolean).join(" · ")),
      i.item?.secret && h("p.muted.small", "What it does is secret: the party doesn't know yet."),
      i.value_gp > 0 && h("p.muted.small", `Worth ${worth(i.value_gp)}${i.qty !== 1 ? ` each, ${worth(i.value_gp * i.qty)} in all` : ""}`),
      world && h("p.small", world),
      x.kind === "character" && h("div.item-toggles", toggle("equipped", "Equipped"),
        i.requires_attunement && toggle("attuned", "Attuned")),
      h("div.hp-buttons",
        takers.length > 0 && h("button.ok-solid", { onclick: () => after(dialog, () => this.claim(i)) }, "Claim"),
        x.can_edit && x.id !== null && h("button", {
          onclick: () => after(dialog, async () => {
            const n = await askQty(`Put ${i.name} back with the party's things`, i.qty, "Put back");
            if (n) this.send("POST", `/items/${i.id}/move`, { to: null, qty: n });
          }),
        }, "Put back"),
        x.can_edit && h("button", { onclick: () => after(dialog, () => this.give(i, x)) }, "Give…")),
      h("div.hp-buttons.second",
        x.can_edit && h("button", { onclick: () => after(dialog, () => this.editSheet(i)) }, "Edit"),
        this.data.can_define && !(i.item && !i.item.id) && h("button", { onclick: () => after(dialog, () => this.defineSheet(i)) },
          i.item?.defined || i.item?.secret ? "What it does…" : "Make it do something…")));
  }

  addSheet(x) {
    this.itemForm(x.kind === "party" ? "Add loot" : `Add to ${x.name}'s things`, { name: "", qty: 1, notes: "", value_gp: 0 },
      (next) => this.send("POST", "/items", { ...next, holder: x.id }));
  }

  editSheet(i) {
    this.itemForm(i.name, i, (next) => this.send("PATCH", `/items/${i.id}`, next),
      () => confirm(`Remove ${i.name} from the party's things (sold, used up, lost)?`) && this.send("DELETE", `/items/${i.id}`));
  }

  itemForm(title, item, save, remove = null) {
    const name = h("input", { value: item.name, placeholder: "Item", "aria-label": "Item", required: true });
    const qty = h("input", { type: "number", min: 1, value: item.qty, "aria-label": "How many", inputmode: "numeric" });
    const notes = h("input", { value: item.notes, placeholder: "Notes (optional)", "aria-label": "Notes" });
    const value = h("input", { type: "number", min: 0, step: "any", value: item.value_gp || "", placeholder: "gp each",
      "aria-label": "Worth, in gold pieces each", inputmode: "decimal" });
    const dialog = bottomSheet(title,
      h("form.item-form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!name.value.trim()) return name.focus();
          const next = { name: name.value.trim(), qty: Math.max(1, Math.trunc(Number(qty.value) || 1)),
            notes: notes.value.trim(), value_gp: Math.max(0, Number(value.value) || 0) };
          if (await save(next)) dialog.close();
        },
      },
        h("div.item-fields", name, qty), h("div.item-fields", notes, value),
        item.item?.defined && h("p.muted.small", "Its worth comes from what it is, when that says."),
        h("div.hp-buttons",
          remove && h("button.danger", { type: "button", onclick: async () => { if (await remove()) dialog.close(); } }, "Remove"),
          h("button.primary", { type: "submit" }, remove ? "Save" : "Add"))));
    if (!remove) name.focus();
  }

  // What an item does: its world item's definition, shared by every copy the party has.
  defineSheet(i) {
    const draft = {
      requires_attunement: !!i.requires_attunement, value_gp: i.item?.defined ? i.value_gp : i.value_gp || 0,
      bonuses: structuredClone(i.bonuses || []), party_knows: !i.item?.secret,
    };
    const name = h("input", { value: i.item?.name || i.name, "aria-label": "What it is",
      placeholder: "What it is, e.g. Cloak of Protection", disabled: !!i.item?.id });
    const rows = h("div.rows.bonuses");
    const drawRows = () => rows.replaceChildren(...draft.bonuses.map((b, n) => h("div.row.bonus",
      { style: "grid-template-columns:1fr 5.5rem auto" },
      h("select", { "aria-label": "Applies to", value: b.target, onchange: (e) => (b.target = e.target.value) },
        BONUS_TARGETS.map(([group, opts]) => h("optgroup", { label: group }, opts.map(([v, l]) => h("option", { value: v }, l))))),
      h("input", { value: b.amount, "aria-label": "Amount", placeholder: "+1",
        onchange: (e) => { const v = e.target.value.trim(); b.amount = /^[+-]?\d+$/.test(v) ? Number(v) : v; } }),
      h("button.icon", { type: "button", "aria-label": "Remove bonus", onclick: () => { draft.bonuses.splice(n, 1); drawRows(); } }, "×"))));
    drawRows();
    const check = (key, label, invert = false) => h("label.play-toggle", h("input", {
      type: "checkbox", checked: invert ? !draft[key] : draft[key],
      onchange: (e) => (draft[key] = invert ? !e.target.checked : e.target.checked),
    }), label);
    const value = h("input", { type: "number", min: 0, step: "any", value: draft.value_gp || "", placeholder: "gp each",
      "aria-label": "Worth, in gold pieces each", onchange: (e) => (draft.value_gp = Math.max(0, Number(e.target.value) || 0)) });
    const dialog = bottomSheet(`What ${i.name} does`,
      h("p.muted.small", "This is what the item is in the world, shared by every one the party has: change it here and it changes for all of them."),
      h("label.field", h("span", "It is"), name),
      h("span.muted.small", "While equipped it adds"),
      rows,
      h("button.add", { type: "button", onclick: () => { draft.bonuses.push({ target: "ac", amount: 1 }); drawRows(); } }, "+ Add bonus"),
      h("div.item-toggles", check("requires_attunement", "Needs attunement")),
      h("label.field", h("span", "Worth (gp each)"), value),
      this.data.dm && h("div.item-toggles", check("party_knows", "Secret: the party doesn't know yet", true)),
      h("div.hp-buttons",
        i.item && h("button.danger", { type: "button", onclick: async () => {
          if (await this.send("DELETE", `/items/${i.id}/definition`)) dialog.close();
        } }, "Make it plain"),
        h("button.primary", { onclick: async () => {
          const body = { ...draft, name: name.value.trim() || i.name };
          if (await this.send("PUT", `/items/${i.id}/definition`, body)) dialog.close();
        } }, "Save")));
  }
}
