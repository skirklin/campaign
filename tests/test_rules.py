"""The SRD's rules for things, found by the names the table gives them."""

from campaign import rules


def names(found):
    return [(r["name"], r.get("note")) for r in found]


def test_exact_names_give_or_take_case_plurals_and_spelling():
    assert names(rules.about("potion of healing")) == [("Potion of Healing", None)]
    assert names(rules.about("Short sword")) == [("Shortsword", None)]
    assert names(rules.about("Light crossbow")) == [("Crossbow, light", None)]
    assert names(rules.about("Studded leather armor")) == [("Studded leather", None)]
    assert rules.about("Javelin")[0]["text"].startswith("**1d6 piercing** · Thrown (range 30/120)")
    # The shield before the spell, unless it's a scroll of it.
    assert rules.about("Shield")[0]["kind"] == "armor"
    assert rules.about("Scroll of Shield")[0]["kind"] == "spell"


def test_a_scroll_is_its_spell_at_the_scrolls_dc():
    found = rules.about("Scroll of Sending")
    assert names(found) == [("Sending", "From a spell scroll (3rd level): save DC 15, +7 to hit."), ("Spell Scroll", None)]
    assert rules.about("Scroll of Water Walking")[0]["name"] == "Water Walk"


def test_families_and_what_a_thing_is_underneath():
    assert names(rules.about("+1 Arrows")) == [
        ("Ammunition, +1, +2, or +3", "+1 ammunition: +1 to attack and damage rolls."), ("Arrows (20)", "What it's made from: arrows.")]
    assert names(rules.about("Dragon Greataxe +1"))[1] == ("Greataxe", "What it's made from: greataxe.")
    assert names(rules.about("Potion of Thunder Resistance")) == [("Potion of Resistance", "Resistance to thunder damage.")]
    assert names(rules.about("Ring of Resistance (Fire)")) == [("Ring of Resistance", "Resistance to fire damage.")]
    assert names(rules.about("Very nice dagger")) == [("Dagger", "In the rules, a dagger.")]
    assert names(rules.about("Set of black leather armor")) == [("Leather", "In the rules, leather armor.")]
    assert names(rules.about("Trident of Adamantium")) == [("Trident", "In the rules, a trident.")]


def test_what_the_rules_dont_have():
    for name in ("Crown of the Dragonlords", "Map of the Ultros", "Goat", "Set of keys", ""):
        assert rules.about(name) == [], name
    assert [r["name"] for r in rules.search("fire resistance")][:1] == ["Armor of Resistance"]
