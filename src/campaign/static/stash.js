// The party stash: what the party holds in common. Shown on the Party tab
// and, for one character, on the Play view's Gear tab.
//
// Anyone in the campaign can change it. Coin goes in and out as changes
// (so two people at once both count); other edits send the whole stash
// with the version they started from, and the server refuses a stale one.
// Taking something moves it into a character's inventory in one step.

import { api, h, toast, bottomSheet, keypad, askQty, worth } from "./util.js";
import { COINS } from "./sheet.js";

export class StashView {
  // `character`: take into this character, and offer nothing else (the Play view).
  // `onCharacter(payload)`: a move changed that character's sheet.
  // `beforeMove()`: save anything pending on the character first.
  constructor(slug, { character = null, onCharacter = null, beforeMove = null } = {}) {
    this.base = `/api/campaigns/${slug}/stash`;
    this.character = character;
    this.onCharacter = onCharacter;
    this.beforeMove = beforeMove;
    this.el = h("section.play-block.stash", h("p.muted", "Loading the party stash…"));
    this.load();
  }

  async load() {
    try {
      this.show(await api("GET", this.base));
    } catch (err) {
      this.el.replaceChildren(h("p.muted", err.message));
    }
  }

  show(data) {
    this.data = data;
    const takers = this.character ? data.characters.filter((c) => c.id === this.character) : data.characters.filter((c) => c.can_edit);
    if (!takers.some((c) => c.id === this.target)) this.target = takers[0]?.id ?? null;
    this.render(takers);
  }

  render(takers) {
    const { stash, log } = this.data;
    const total = stash.items.reduce((sum, i) => sum + (i.value_gp || 0) * i.qty, 0);
    const pick = !this.character && takers.length > 1 && h("label.stash-target", "Taking for ",
      h("select", { value: String(this.target), onchange: (e) => (this.target = Number(e.target.value)) },
        takers.map((c) => h("option", { value: String(c.id) }, c.name))));
    this.el.replaceChildren(...[
      h("div.block-head", h("h2", "Party stash"),
        h("span.muted.small", total ? `items worth ${worth(total)}` : "shared by the party")),
      pick,
      h("div.play-coins", COINS.map((c) => h("button.play-coin", { onclick: () => this.coinSheet(c) },
        h("strong", stash.currency[c] || 0), h("span", c)))),
      stash.items.length === 0
        ? h("p.empty", "Nothing in the stash. Loot nobody's claimed goes here.")
        : h("div.play-list", stash.items.map((item, i) => h("div.play-row.item",
            h("button.link.row-main", { onclick: () => this.itemSheet(i), "aria-label": `Edit ${item.name}` },
              h("span", item.name, item.qty !== 1 && h("span.muted", ` ×${item.qty}`),
                item.value_gp > 0 && h("span.muted", ` · ${worth(item.value_gp)}${item.qty !== 1 ? " each" : ""}`)),
              item.notes && h("small.muted", item.notes)),
            this.target && h("button", { onclick: () => this.take(item) }, "Take")))),
      h("button.add", { onclick: () => this.itemSheet(null) }, "+ Add to stash"),
      log.length > 0 && h("details.stash-log",
        h("summary.muted.small", "History"),
        h("ul.log", log.map((l) => h("li", h("time", l.created_at.replace("T", " ").slice(0, 16)), " ", l.reason,
          l.who && h("span.muted", ` · ${l.who.split("@")[0]}`))))),
    ].filter(Boolean));
  }

  // -- changes -----------------------------------------------------------------

  async send(method, url, body) {
    try {
      const r = await api(method, url, body);
      if (r.character) {
        this.onCharacter?.(r.character);
        this.show(r.stash);
      } else {
        this.show(r);
      }
      return true;
    } catch (err) {
      toast(err.message, true);
      this.load();
      return false;
    }
  }

  // Into the stash (to = "stash") or out of it into the target character (to = "character").
  async move(to, items = {}, currency = {}) {
    if (!this.target) return toast("You don't have a character in this campaign to take it.", true);
    await this.beforeMove?.();
    return this.send("POST", `${this.base}/move`, { character: this.target, to, items, currency });
  }

  async take(item) {
    const n = await askQty(`Take ${item.name}`, item.qty, "Take");
    if (n) this.move("character", { [item.name]: n });
  }

  coinSheet(coin) {
    let amount = 0;
    const go = (fn) => () => { if (!amount) return; dialog.close(); fn(amount); };
    const change = (sign) => (n) => this.send("POST", `${this.base}/coin`, { currency: { [coin]: sign * n } });
    const who = this.data.characters.find((c) => c.id === this.target)?.name;
    const dialog = bottomSheet(`Party ${coin.toUpperCase()}: ${this.data.stash.currency[coin] || 0}`,
      ...keypad((n) => (amount = n)),
      h("div.hp-buttons",
        h("button.danger-solid", { onclick: go(change(-1)) }, "Spend"),
        h("button.ok-solid", { onclick: go(change(1)) }, "Add")),
      this.target && h("div.hp-buttons.second",
        h("button", { onclick: go((n) => this.move("character", {}, { [coin]: n })) }, `Take for ${who}`),
        h("button", { onclick: go((n) => this.move("stash", {}, { [coin]: n })) }, `Put in from ${who}`)));
  }

  // Add an item, or edit or remove one.
  itemSheet(i) {
    const item = i === null ? { name: "", qty: 1, notes: "", value_gp: 0 } : this.data.stash.items[i];
    const name = h("input", { value: item.name, placeholder: "Item", "aria-label": "Item", required: true });
    const qty = h("input", { type: "number", min: 0, value: item.qty, "aria-label": "How many", inputmode: "numeric" });
    const notes = h("input", { value: item.notes, placeholder: "Notes (optional)", "aria-label": "Notes" });
    const value = h("input", { type: "number", min: 0, step: "any", value: item.value_gp || "", placeholder: "gp each",
      "aria-label": "Worth, in gold pieces each", inputmode: "decimal" });
    const save = async (items, reason) => {
      const stash = { ...this.data.stash, items };
      if (await this.send("PUT", this.base, { stash, reason, version: this.data.version })) dialog.close();
    };
    const dialog = bottomSheet(i === null ? "Add to the stash" : item.name,
      h("form.stash-form", {
        onsubmit: (e) => {
          e.preventDefault();
          const next = { name: name.value.trim(), qty: Math.max(0, Math.trunc(Number(qty.value) || 0)), notes: notes.value.trim(),
            value_gp: Math.max(0, Number(value.value) || 0) };
          if (!next.name) return name.focus();
          const items = [...this.data.stash.items];
          // Keep what this form doesn't show (an item's bonuses, attunement).
          if (i === null) items.push(next); else items[i] = { ...items[i], ...next };
          save(items.filter((x) => x.qty > 0), i === null ? `added ${next.name}${next.qty !== 1 ? ` ×${next.qty}` : ""}` : `edited ${next.name}`);
        },
      },
        h("div.stash-fields", name, qty), h("div.stash-fields", notes, value),
        h("div.hp-buttons",
          i !== null && h("button.danger", {
            type: "button", onclick: () => save(this.data.stash.items.filter((_, j) => j !== i), `removed ${item.name}`),
          }, "Remove"),
          h("button.primary", { type: "submit" }, i === null ? "Add" : "Save"))));
    if (i === null) name.focus();
  }
}
