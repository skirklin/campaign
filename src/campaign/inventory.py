"""The party's inventory: everything the party owns, who has it, and what it does.

**Holdings.** Every item the party owns is a holding: a name, how many, and
who has it. The holder is the party itself (loot nobody has claimed yet), a
character (their world entity), or anyone else in the world (the trident
left with Vulkan to be reforged). Claiming, giving and putting back are all
the same thing: the holding changes hands. There's one list, so it's always
clear what the party has and where it is.

**Definitions.** A holding of an item that does something points at the
world item that defines it: its bonuses, whether it needs attunement and
what one is worth (charsheet.ItemMechanics), kept as a "mechanics" fact on
that world entity. Nothing is copied: every holding of a Potion of Healing
or the one Crown of Dragon Lords reads the same definition, and fixing it
fixes it everywhere. Because it's a fact, it follows the world's spoiler
rules: the DM can give an item its true mechanics while the party hasn't
identified it, and players see (and their sheets count) only what the party
knows. Mundane loot ("2 goats") is a holding with no definition.

**Coin** is in purses, one per holder, the party's included.

Every change is logged with the session it happened in, and an item's
moves show on its world timeline. Who may change what is the web layer's
business (web.py); this module does what it's asked.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .charsheet import COINS, GP_PER_COIN, CharacterSheet, Gear, Item, ItemMechanics, no_coins
from .db import Campaign, Store, now
from .world import World

PARTY_NAMES = {"", "party", "the party", "stash", "the stash", "party stash", "the party stash", "unclaimed"}


class InventoryError(ValueError):
    """A change that can't be made, said so a person can fix it."""


def coins_gp(coins: dict[str, int]) -> float:
    return sum(GP_PER_COIN.get(k, 0) * v for k, v in coins.items())


def coin_text(coins: dict[str, int]) -> str:
    return ", ".join(f"{v} {k}" for k, v in coins.items() if v) or "nothing"


class Inventory:
    """One campaign's inventory, as one person sees it (`campaign.role` decides what definitions show)."""

    def __init__(self, store: Store, campaign: Campaign, user_id: int | None = None):
        self.store = store
        self.campaign = campaign
        self.conn = store.conn
        self.world = World(store, campaign)
        self.user_id = user_id

    # -- holders ------------------------------------------------------------------

    def character_holder(self, character_id: int) -> int:
        return self.store.link_character(character_id)

    def holder_name(self, holder_id: int | None) -> str:
        if holder_id is None:
            return "the party"
        row = self.conn.execute("SELECT name FROM entities WHERE id = ?", (holder_id,)).fetchone()
        return row["name"] if row else "someone"

    def character_of(self, holder_id: int | None) -> dict[str, Any] | None:
        """The character a holder is, if it's one of the party's: {id, name}."""
        return None if holder_id is None else self.store.character_for_entity(holder_id)

    def resolve_holder(self, name: str | None, create_kind: str | None = "npc") -> int | None:
        """Who `name` is, as a holder: None for the party, a character's entity, or anyone in the world.

        Someone the world doesn't know yet is added as `create_kind` (an NPC,
        say), or InventoryError if that's None.
        """
        if name is None or name.strip().lower() in PARTY_NAMES:
            return None
        if ch := self.store.find_character(name, self.campaign):
            return self.character_holder(ch["id"])
        if e := self.world.resolve(name):
            return e["id"]
        if create_kind is None:
            raise InventoryError(f"nobody called {name!r} in this campaign")
        return self.world.ensure_entity(name, create_kind)["id"]

    # -- definitions --------------------------------------------------------------

    def mechanics(self, item_id: int | None) -> ItemMechanics | None:
        """An item's definition as this viewer may see it; None if there's none (or the party doesn't know it)."""
        if item_id is None:
            return None
        values = self.world.attribute_values(item_id, "mechanics")
        return ItemMechanics.model_validate_json(values[-1]) if values else None

    def find_item(self, name: str) -> int | None:
        """The world item called `name` (by name or alias), if there is one."""
        found = self.world.resolve(name, "item")
        return found["id"] if found and found["kind"] == "item" else None

    def visible(self, entity_id: int) -> bool:
        """Whether this viewer may know the world has this thing: anything about it the party knows."""
        if not self.campaign.spoiler_safe:
            return True
        return self.conn.execute(
            "SELECT 1 FROM facts WHERE entity_id = ? AND party_knows_from IS NOT NULL AND party_knows_from <= ?"
            " UNION SELECT 1 FROM event_entities WHERE entity_id = ? LIMIT 1",
            (entity_id, self.world.now, entity_id)).fetchone() is not None

    def secret(self, item_id: int | None) -> bool:
        """Whether the item's definition is one the party doesn't know yet (only the DM sees it)."""
        return item_id is not None and self.conn.execute(
            "SELECT 1 FROM facts WHERE entity_id = ? AND attribute = 'mechanics' AND valid_to IS NULL"
            " AND modality = 'fact' AND party_knows_from IS NULL", (item_id,),
        ).fetchone() is not None

    def define(
        self, name: str, mechanics: ItemMechanics, party_knows: bool = True, item_id: int | None = None,
        run_id: int | None = None,
    ) -> int:
        """Say what a world item does, making the item if the world doesn't have it. Returns its id.

        A player can't overwrite a definition the DM has kept from the party.
        """
        if item_id is None:
            item_id = self.find_item(name) or self.world._new_entity(name, "item")["id"]
        if self.campaign.spoiler_safe and self.secret(item_id):
            raise InventoryError("the DM has said what this item does; ask them to change it")
        party_knows = party_knows or self.campaign.spoiler_safe  # what a player writes, the party knows
        current = self.conn.execute(
            "SELECT value, party_knows_from FROM facts WHERE entity_id = ? AND attribute = 'mechanics'"
            " AND valid_to IS NULL AND modality = 'fact'", (item_id,),
        ).fetchone()
        if current and current["value"] == mechanics.model_dump_json() and (current["party_knows_from"] is not None) == party_knows:
            return item_id
        entity = self.world.entity(item_id)
        run = run_id or self.world.start_run("manual", session=self.world.now, label=f"what {entity['name']} does")
        self.world.record(item_id, f"{entity['name']}: {mechanics.describe()}", run, self.world.now,
                          attribute="mechanics", value=mechanics.model_dump_json(), party_knows=party_knows)
        return item_id

    # -- reading --------------------------------------------------------------------

    def _row(self, holding_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM holdings WHERE id = ? AND campaign_id = ?",
                                (holding_id, self.campaign.id)).fetchone()
        if row is None:
            raise InventoryError(f"no item {holding_id} in the party's inventory")
        return row

    def holding(self, holding_id: int) -> dict[str, Any]:
        return self._view(self._row(holding_id))

    def _view(self, row: sqlite3.Row) -> dict[str, Any]:
        """A holding as this viewer sees it: its definition only if they may, its value from the definition first."""
        m = self.mechanics(row["item_id"])
        item = None
        if row["item_id"] is not None:
            entity = self.world.entity(row["item_id"])
            visible = self.visible(row["item_id"])
            item = {
                "id": row["item_id"] if visible else None,
                "name": entity["name"] if entity and visible else None,
                "defined": m is not None,
                "secret": not self.campaign.spoiler_safe and self.secret(row["item_id"]),
                **(m.model_dump() if m else {}),
            }
        needs = bool(m and m.requires_attunement)
        return {
            "id": row["id"], "name": row["name"], "qty": row["qty"], "notes": row["notes"],
            "holder_id": row["holder_id"], "equipped": bool(row["equipped"]), "attuned": bool(row["attuned"]),
            "value_gp": (m.value_gp if m and m.value_gp else row["value_gp"]),
            "requires_attunement": needs, "bonuses": [b.model_dump() for b in m.bonuses] if m else [],
            "active": bool(row["equipped"]) and (bool(row["attuned"]) or not needs),
            "item": item,
        }

    def holdings(self, holder_id: int | None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM holdings WHERE campaign_id = ? AND holder_id IS ? ORDER BY id", (self.campaign.id, holder_id))
        return [self._view(r) for r in rows]

    def purse(self, holder_id: int | None) -> dict[str, int]:
        row = self.conn.execute("SELECT coins FROM purses WHERE campaign_id = ? AND holder_id IS ?",
                                (self.campaign.id, holder_id)).fetchone()
        return no_coins() | (json.loads(row["coins"]) if row else {})

    def overview(self) -> dict[str, Any]:
        """Everything the party owns, by who has it: the party's own first, then each character, then anyone else."""
        holders: list[dict[str, Any]] = [{"id": None, "name": "The party", "kind": "party", "character_id": None}]
        for ch in self.store.characters(campaign_ids=[self.campaign.id]):
            holders.append({"id": self.character_holder(ch["id"]), "name": ch["name"], "kind": "character",
                            "character_id": ch["id"]})
        known = {h["id"] for h in holders}
        others = self.conn.execute(
            "SELECT DISTINCT e.id, e.name FROM entities e WHERE e.campaign_id = ? AND e.id IN"
            " (SELECT holder_id FROM holdings WHERE campaign_id = ? UNION SELECT holder_id FROM purses WHERE campaign_id = ?)"
            " ORDER BY e.name", (self.campaign.id, self.campaign.id, self.campaign.id))
        holders += [{"id": r["id"], "name": r["name"], "kind": "other", "character_id": None}
                    for r in others if r["id"] not in known]
        total = 0.0
        for h in holders:
            h["items"] = self.holdings(h["id"])
            h["purse"] = self.purse(h["id"])
            h["worth_gp"] = round(sum(i["value_gp"] * i["qty"] for i in h["items"]), 2)
            total += h["worth_gp"] + coins_gp(h["purse"])
        holders = [h for h in holders if h["kind"] != "other" or h["items"] or any(h["purse"].values())]
        return {"holders": holders, "worth_gp": round(total, 2)}

    def gear(self, holder_id: int) -> list[Gear]:
        """What a character has equipped whose bonuses count, as this viewer knows them."""
        return [Gear(name=h["name"], bonuses=h["bonuses"]) for h in self.holdings(holder_id) if h["active"] and h["bonuses"]]

    def sheet(self, character: dict[str, Any]) -> CharacterSheet:
        """A character's sheet, counting what they have equipped."""
        sheet = CharacterSheet.model_validate(character["sheet"])
        return sheet.with_gear(self.gear(self.character_holder(character["id"])))

    def carried(self, holder_id: int) -> list[Item]:
        """What someone has, as a sheet file carries it: each item with its definition (as this viewer knows it)."""
        return [Item(name=h["name"], qty=h["qty"], equipped=h["equipped"], attuned=h["attuned"],
                     requires_attunement=h["requires_attunement"], bonuses=h["bonuses"], notes=h["notes"],
                     value_gp=h["value_gp"]) for h in self.holdings(holder_id)]

    def log(self, holder_id: int | None = None, everyone: bool = True, limit: int = 40) -> list[dict[str, Any]]:
        """Recent changes, newest first: all of them, or those to or from one holder."""
        sql = ("SELECT l.session, l.text, l.created_at, u.email AS who FROM inventory_log l"
               " LEFT JOIN users u ON u.id = l.user_id WHERE l.campaign_id = ?")
        params: list[Any] = [self.campaign.id]
        if not everyone:
            sql += " AND ? IN (l.from_holder, l.to_holder)"
            params.append(holder_id)
        rows = self.conn.execute(sql + " ORDER BY l.id DESC LIMIT ?", [*params, limit])
        return [dict(r) for r in rows]

    # -- writing --------------------------------------------------------------------

    def _log(self, text: str, item_id: int | None = None, from_holder: int | None = None,
             to_holder: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO inventory_log (campaign_id, session, text, item_id, from_holder, to_holder, user_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.campaign.id, self.world.now, text, item_id, from_holder, to_holder, self.user_id, now()))

    @staticmethod
    def _label(name: str, qty: int) -> str:
        return f"{name}{f' ×{qty}' if qty != 1 else ''}"

    def _stack(self, holder_id: int | None, item_id: int | None, name: str, notes: str, value_gp: float,
               exclude: int | None = None) -> sqlite3.Row | None:
        """A holding the same holder already has that one like this would join: the same thing, said the same way."""
        return self.conn.execute(
            "SELECT * FROM holdings WHERE campaign_id = ? AND holder_id IS ? AND item_id IS ? AND name = ? COLLATE NOCASE"
            " AND notes = ? AND value_gp = ? AND id IS NOT ? ORDER BY id LIMIT 1",
            (self.campaign.id, holder_id, item_id, name.strip(), notes.strip(), value_gp, exclude),
        ).fetchone()

    def add(
        self, name: str, qty: int = 1, holder_id: int | None = None, notes: str = "", value_gp: float = 0,
        item_id: int | None = None, link: bool = True, reason: str = "",
    ) -> int:
        """Something new for the party (loot, a purchase): held by `holder_id`, or the party's if None.

        With `link`, an item the world already defines by that name is linked
        to its definition. Joins a holding of the same thing the holder has.
        """
        name = name.strip()
        if not name:
            raise InventoryError("an item needs a name")
        if qty < 1:
            raise InventoryError("how many must be at least 1")
        if value_gp < 0:
            raise InventoryError("an item can't be worth less than nothing")
        if item_id is None and link:
            item_id = self.find_item(name)
        if item_id is not None and self.mechanics(item_id) and self.mechanics(item_id).value_gp:
            value_gp = 0  # the definition says
        stamp = now()
        with self.conn:
            if same := self._stack(holder_id, item_id, name, notes, value_gp):
                self.conn.execute("UPDATE holdings SET qty = qty + ?, updated_at = ? WHERE id = ?", (qty, stamp, same["id"]))
                holding_id = same["id"]
            else:
                holding_id = self.conn.execute(
                    "INSERT INTO holdings (campaign_id, holder_id, item_id, name, qty, notes, value_gp, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.campaign.id, holder_id, item_id, name, qty, notes.strip(), value_gp, stamp, stamp),
                ).lastrowid
            who = self.holder_name(holder_id)
            self._log(f"{self._label(name, qty)} for {who}" + (f" ({reason})" if reason else ""),
                      item_id, None, holder_id)
        return holding_id

    def change(self, holding_id: int, reason: str = "", **fields: Any) -> str:
        """Edit a holding: name, qty, notes, value_gp, equipped, attuned. A qty of 0 removes it."""
        row = self._row(holding_id)
        allowed = {"name", "qty", "notes", "value_gp", "equipped", "attuned"}
        if unknown := set(fields) - allowed:
            raise InventoryError(f"can't change {', '.join(sorted(unknown))}")
        fields = {k: v for k, v in fields.items() if v is not None}
        if fields.get("qty") == 0:
            return self.remove(holding_id, reason)
        if "name" in fields:
            fields["name"] = str(fields["name"]).strip()
            if not fields["name"]:
                raise InventoryError("an item needs a name")
        if fields.get("qty", 1) < 0 or fields.get("value_gp", 0) < 0:
            raise InventoryError("not less than nothing")
        changes = {k: v for k, v in fields.items() if row[k] != (int(v) if isinstance(v, bool) else v)}
        if not changes:
            return "nothing to change"
        with self.conn:
            self.conn.execute(
                f"UPDATE holdings SET {', '.join(f'{k} = ?' for k in changes)}, updated_at = ? WHERE id = ?",
                (*changes.values(), now(), holding_id))
            name = changes.get("name", row["name"])
            words = []
            for k, v in changes.items():
                if k in ("equipped", "attuned"):
                    words.append(k if v else f"un{k}")
                elif k == "qty":
                    words.append(f"now ×{v}")
                elif k == "name":
                    words.append(f"renamed from {row['name']}")
                elif k == "value_gp":
                    words.append(f"worth {v:g} gp each")
                else:
                    words.append("notes changed")
            text = f"{self.holder_name(row['holder_id'])}'s {name}: {', '.join(words)}" if row["holder_id"] else \
                f"The party's {name}: {', '.join(words)}"
            self._log(text + (f" ({reason})" if reason else ""), row["item_id"], row["holder_id"], row["holder_id"])
        return text

    def remove(self, holding_id: int, reason: str = "") -> str:
        """Gone from the party: sold, used up, lost."""
        row = self._row(holding_id)
        text = f"{self._label(row['name'], row['qty'])} gone from {self.holder_name(row['holder_id'])}" \
            + (f" ({reason})" if reason else "")
        with self.conn:
            self.conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
            self._log(text, row["item_id"], row["holder_id"], None)
        return text

    def move(self, holding_id: int, to: int | None, qty: int | None = None, reason: str = "") -> str:
        """Hand some or all of a holding to someone else: claiming it, giving it, putting it back.

        It arrives unequipped and joins anything the same they already have.
        """
        row = self._row(holding_id)
        qty = row["qty"] if qty is None else qty
        if qty < 1:
            raise InventoryError("move at least one")
        if qty > row["qty"]:
            raise InventoryError(f"only {row['qty']} × {row['name']} to give")
        if to == row["holder_id"]:
            raise InventoryError(f"{self.holder_name(to)} already has it")
        stamp = now()
        with self.conn:
            if same := self._stack(to, row["item_id"], row["name"], row["notes"], row["value_gp"]):
                self.conn.execute("UPDATE holdings SET qty = qty + ?, updated_at = ? WHERE id = ?", (qty, stamp, same["id"]))
                if qty == row["qty"]:
                    self.conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
            elif qty == row["qty"]:
                self.conn.execute("UPDATE holdings SET holder_id = ?, equipped = 0, attuned = 0, updated_at = ? WHERE id = ?",
                                  (to, stamp, holding_id))
            else:
                self.conn.execute(
                    "INSERT INTO holdings (campaign_id, holder_id, item_id, name, qty, notes, value_gp, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.campaign.id, to, row["item_id"], row["name"], qty, row["notes"], row["value_gp"], stamp, stamp))
            if qty < row["qty"]:
                self.conn.execute("UPDATE holdings SET qty = qty - ?, updated_at = ? WHERE id = ?", (qty, stamp, holding_id))
            text = self._move_text(self._label(row["name"], qty), row["holder_id"], to) + (f" ({reason})" if reason else "")
            self._log(text, row["item_id"], row["holder_id"], to)
        return text

    def _move_text(self, what: str, src: int | None, dst: int | None) -> str:
        if src is None:
            return f"{self.holder_name(dst)} took {what}"
        if dst is None:
            return f"{self.holder_name(src)} put {what} back in the party's things"
        return f"{self.holder_name(src)} gave {what} to {self.holder_name(dst)}"

    def unlink(self, holding_id: int) -> None:
        """Make a holding plain again, keeping what it's worth."""
        row = self._row(holding_id)
        if self.campaign.spoiler_safe and self.secret(row["item_id"]):
            raise InventoryError("the DM has said what this item is; ask them to change it")
        value = self._view(row)["value_gp"]
        with self.conn:
            self.conn.execute("UPDATE holdings SET item_id = NULL, value_gp = ?, updated_at = ? WHERE id = ?",
                              (value, now(), holding_id))

    def link(self, holding_id: int, item_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE holdings SET item_id = ?, updated_at = ? WHERE id = ?", (item_id, now(), holding_id))

    # -- coin -----------------------------------------------------------------------

    def _set_purse(self, holder_id: int | None, coins: dict[str, int]) -> None:
        self.conn.execute("DELETE FROM purses WHERE campaign_id = ? AND holder_id IS ?", (self.campaign.id, holder_id))
        self.conn.execute("INSERT INTO purses (campaign_id, holder_id, coins) VALUES (?, ?, ?)",
                          (self.campaign.id, holder_id, json.dumps(coins)))

    def _check_coins(self, coins: dict[str, int]) -> dict[str, int]:
        if bad := set(coins) - set(COINS):
            raise InventoryError(f"coins are {', '.join(COINS)}, not {', '.join(sorted(bad))}")
        return {k: int(v) for k, v in coins.items() if v}

    def coin(self, holder_id: int | None, change: dict[str, int], reason: str = "") -> str:
        """Gain (positive) or spend (negative) coin. A change, not a total, so two at once both count."""
        change = self._check_coins(change)
        if not change:
            raise InventoryError("nothing to change")
        purse = self.purse(holder_id)
        for k, d in change.items():
            if purse[k] + d < 0:
                raise InventoryError(f"{self.holder_name(holder_id)} only has {purse[k]} {k}")
            purse[k] += d
        words = ", ".join(f"{'+' if d > 0 else '−'}{abs(d)} {k}" for k, d in change.items())
        who = self.holder_name(holder_id)
        text = f"{who[0].upper()}{who[1:]}: {words}" + (f" ({reason})" if reason else "")
        with self.conn:
            self._set_purse(holder_id, purse)
            self._log(text, None, holder_id, holder_id)
        return text

    def give_coin(self, src: int | None, dst: int | None, coins: dict[str, int], reason: str = "") -> str:
        coins = self._check_coins(coins)
        if not coins or any(v < 0 for v in coins.values()):
            raise InventoryError("give some coin")
        if src == dst:
            raise InventoryError("that's the same purse")
        have, get = self.purse(src), self.purse(dst)
        for k, n in coins.items():
            if have[k] < n:
                raise InventoryError(f"{self.holder_name(src)} only has {have[k]} {k}")
            have[k] -= n
            get[k] += n
        text = self._move_text(coin_text(coins), src, dst) + (f" ({reason})" if reason else "")
        with self.conn:
            self._set_purse(src, have)
            self._set_purse(dst, get)
            self._log(text, None, src, dst)
        return text

    def split(self, reason: str = "") -> str:
        """Share the party's purse evenly among the characters; what doesn't divide stays with the party."""
        characters = self.store.characters(campaign_ids=[self.campaign.id])
        if not characters:
            raise InventoryError("there are no characters to share it with")
        pot = self.purse(None)
        each = {k: v // len(characters) for k, v in pot.items()}
        if not any(each.values()):
            raise InventoryError("not enough in the party's purse to share")
        with self.conn:
            for ch in characters:
                holder = self.character_holder(ch["id"])
                purse = self.purse(holder)
                self._set_purse(holder, {k: purse[k] + each[k] for k in COINS})
            self._set_purse(None, {k: pot[k] - each[k] * len(characters) for k in COINS})
            text = f"Shared the party's coin: {coin_text(each)} each to {len(characters)}" + (f" ({reason})" if reason else "")
            self._log(text)
        return text

    # -- characters coming and going ------------------------------------------------

    def bring_in(self, holder_id: int, items: list[Item], currency: dict[str, int], reason: str = "",
                 run_id: int | None = None) -> None:
        """What a character file carries (an import, a scan, a copy): their holdings and purse.

        Items with bonuses or attunement are linked to the world item of that
        name, which is defined from them if the world hasn't said what it does.
        """
        for item in items:
            item_id = None
            if (m := item.mechanics) is not None:
                item_id = self.find_item(item.name)
                if item_id is None or self.mechanics(item_id) is None and not self.secret(item_id):
                    item_id = self.define(item.name, m, item_id=item_id, run_id=run_id)
            holding = self.add(item.name, item.qty, holder_id, item.notes, item.value_gp, item_id=item_id,
                               link=item_id is None, reason=reason)
            if item.equipped or item.attuned:
                self.change(holding, equipped=item.equipped, attuned=item.attuned)
        if coins := {k: v for k, v in self._check_coins(currency).items() if v > 0}:
            self.coin(holder_id, coins, reason)

    def release(self, holder_id: int, reason: str = "") -> None:
        """Someone leaves the party's books (a deleted character): what they had goes back to the party."""
        for h in self.holdings(holder_id):
            self.move(h["id"], None, reason=reason)
        if coins := {k: v for k, v in self.purse(holder_id).items() if v}:
            self.give_coin(holder_id, None, coins, reason)
        with self.conn:
            self.conn.execute("DELETE FROM purses WHERE campaign_id = ? AND holder_id = ?", (self.campaign.id, holder_id))
