from __future__ import annotations

import json
from datetime import UTC, datetime

from clashlens import catalog
from clashlens.army_decoder import DECODER_VERSION, decode_army_share_code
from clashlens.battle import SOURCE_PARSER_VERSION, parse_battle_log


def test_fixture_decodes_exactly() -> None:
    code = "h0p9e14_32d1x53u2x58-1x97s2x2"
    result = decode_army_share_code(code)
    assert not hasattr(result, "category"), f"expected success got failure {result}"
    # home troops: 2 Ice Golem (troop:58) and 1 Apprentice Warden (troop:97)
    home = {(f.typed_id, f.quantity) for f in result.home_troops}
    assert ("troop:58", 2) in home
    assert ("troop:97", 1) in home
    assert len(home) == 2
    # no cc troops, no siege
    assert len(result.cc_troops) == 0
    assert len(result.siege) == 0
    # spells: pooled s and d, but raw origin preserved
    pooled = {(f.typed_id, f.quantity, f.origin) for f in result.spells}
    # should have Rage 2 home, Recall 1 cc
    assert ("spell:2", 2, "home") in pooled
    # check via raw
    assert any(
        f.typed_id == "spell:2" and f.quantity == 2 and f.origin == "home"
        for f in result.home_spells_raw
    )
    assert any(
        f.typed_id == "spell:53" and f.quantity == 1 and f.origin == "clan_castle"
        for f in result.cc_spells_raw
    )
    # pooled includes both
    assert len(result.spells) == 2
    # hero
    assert len(result.heroes) == 1
    h = result.heroes[0]
    assert h.hero_typed_id == "hero:0"
    assert h.pet_typed_id == "pet:9"
    assert set(h.equipment_typed_ids) == {"equipment:14", "equipment:32"}
    # absent items not inferred
    assert all(f.typed_id != "troop:0" for f in result.home_troops)
    assert result.decoder_version == DECODER_VERSION
    # identity excludes cc troops/siege
    assert result.identity_hash is not None


def test_missing_and_empty_code_remain_canonical_but_fail_decode() -> None:
    for code, cat in [(None, "missing_army_share_code"), ("", "empty_army_share_code")]:
        result = decode_army_share_code(code)
        assert result.category == cat
        assert result.decoder_version == DECODER_VERSION

    # parser keeps battle valid
    for payload_code in [None, ""]:
        body = json.dumps(
            {
                "items": [
                    {
                        "battleType": "legend",
                        "attack": True,
                        "battleTimestamp": "2026-08-04T12:00:00Z",
                        "stars": 3,
                        "destructionPercentage": 100,
                        "opponentPlayerTag": "#8PP",
                        "opponentName": "Def",
                        "opponentTownHallLevel": 18,
                        **(
                            {}
                            if payload_code is None
                            else {"armyShareCode": payload_code}
                        ),
                    }
                ]
            }
        ).encode()
        parsed = parse_battle_log(
            body,
            expected_tag="#2PP",
            observed_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
            parser_version=SOURCE_PARSER_VERSION,
        )
        assert parsed.rows[0].outcome == "valid_legend"
        assert parsed.rows[0].battle is not None
        expected = payload_code if payload_code == "" else None
        assert parsed.rows[0].battle.army_share_code == expected
        assert parsed.has_row_gap is False


def test_unnamed_id_is_saved_and_identified_like_any_other() -> None:
    result = decode_army_share_code("u2x58-3x9999")
    assert [(fact.typed_id, fact.quantity) for fact in result.home_troops] == [
        ("troop:58", 2),
        ("troop:9999", 3),
    ]
    assert result.identity_hash is not None
    assert result.identity_hash != decode_army_share_code("u2x58").identity_hash
    # The s section names the kind: spell 58, although troop 58 exists.
    assert [(f.typed_id, f.quantity) for f in decode_army_share_code("s1x58").spells] == [
        ("spell:58", 1)
    ]
    assert decode_army_share_code("u1x58z9").category == "unknown_token"


def test_adding_a_name_to_the_unit_list_changes_no_saved_army(monkeypatch) -> None:
    code = "h6p17e61_49u5x177s1x2"
    named = decode_army_share_code(code)
    monkeypatch.delitem(catalog._CATALOG_ENTRIES, "equipment:61")
    unnamed = decode_army_share_code(code)
    assert unnamed == named
    assert unnamed.heroes[0].equipment_typed_ids == ("equipment:49", "equipment:61")


def test_same_numeric_id_in_two_namespaces_distinct() -> None:
    # troop:2 and spell:2 are different; ensure both valid and identity distinct
    a = decode_army_share_code("u1x2s1x2")
    assert not hasattr(a, "category")
    assert any(f.typed_id == "troop:2" for f in a.home_troops)
    assert any(f.typed_id == "spell:2" for f in a.spells)


def test_each_section_names_its_ids_kind() -> None:
    # Numbers overlap across kinds: 16 is a spell, pet and equipment but no
    # troop; 4 is no spell; 3 is no hero; 28 is no equipment. Each is saved
    # as the kind its section names, never guessed from another kind.
    result = decode_army_share_code("u1x16i2x16s1x4d1x4h3p9e14_28")
    assert [(f.typed_id, f.quantity, f.origin) for f in result.home_troops] == [
        ("troop:16", 1, "home")
    ]
    assert [(f.typed_id, f.quantity, f.origin) for f in result.cc_troops] == [
        ("troop:16", 2, "clan_castle")
    ]
    assert {(f.typed_id, f.origin) for f in result.spells} == {
        ("spell:4", "home"),
        ("spell:4", "clan_castle"),
    }
    assert [
        (hero.hero_typed_id, hero.pet_typed_id, hero.equipment_typed_ids)
        for hero in result.heroes
    ] == [("hero:3", "pet:9", ("equipment:14", "equipment:28"))]
    assert result.identity_hash is not None


def test_catalog_display_name_does_not_change_identity(monkeypatch) -> None:
    before = decode_army_share_code("u2x58")
    monkeypatch.setitem(catalog._CATALOG_ENTRIES["troop:58"], "name", "Renamed")
    after = decode_army_share_code("u2x58")
    assert before.identity_hash == after.identity_hash


def test_encoded_order_permutations_same_identity() -> None:
    a = decode_army_share_code("u2x58-1x97s2x2h0p9e14_32d1x53")
    b = decode_army_share_code("h0p9e14_32d1x53u2x58-1x97s2x2")
    c = decode_army_share_code("s2x2u1x97-2x58d1x53h0p9e14_32")
    assert not hasattr(a, "category")
    assert a.identity_hash == b.identity_hash == c.identity_hash


def test_changing_included_unit_changes_identity() -> None:
    base = decode_army_share_code("h0p9e14_32d1x53u2x58-1x97s2x2")
    altered_qty = decode_army_share_code("h0p9e14_32d1x53u1x58-1x97s2x2")
    altered_pet = decode_army_share_code("h0p0e14_32d1x53u2x58-1x97s2x2")
    altered_eq = decode_army_share_code("h0p9e1_32d1x53u2x58-1x97s2x2")
    assert base.identity_hash != altered_qty.identity_hash
    assert base.identity_hash != altered_pet.identity_hash
    assert base.identity_hash != altered_eq.identity_hash


def test_changing_only_siege_or_cc_troops_does_not_change_identity() -> None:
    base = decode_army_share_code("h0p9e14_32d1x53u2x58-1x97s2x2")
    with_siege = decode_army_share_code("h0p9e14_32d1x53u1x51-2x58-1x97s2x2")
    with_cc = decode_army_share_code(
        "h0p9e14_32d1x53u2x58-1x97i1x0s2x2"
    )  # adds CC troop Barbarian
    assert base.identity_hash == with_siege.identity_hash
    assert base.identity_hash == with_cc.identity_hash
    # but siege fact preserved
    assert len(with_siege.siege) == 1
    assert with_siege.siege[0].typed_id == "troop:51"
    assert len(with_cc.cc_troops) == 1


def test_home_and_cc_spells_pool_while_origin_recoverable() -> None:
    r = decode_army_share_code("s1x2d1x53")
    assert len(r.spells) == 2
    assert any(f.origin == "home" for f in r.spells)
    assert any(f.origin == "clan_castle" for f in r.spells)
    # pooled for identity
    assert len(r.home_spells_raw) == 1
    assert len(r.cc_spells_raw) == 1


def test_cc_troops_and_siege_never_leak_into_home() -> None:
    r = decode_army_share_code("i1x0u1x51")
    assert all(f.origin == "home" for f in r.home_troops) if r.home_troops else True
    assert all(f.origin == "clan_castle" for f in r.cc_troops)
    assert len(r.home_troops) == 0
    assert len(r.cc_troops) == 1
    assert len(r.siege) == 1 and r.siege[0].origin == "home"


def test_hero_modifier_raw_preserved_not_in_identity() -> None:
    a = decode_army_share_code("h0m5p9e14_32u2x58")
    b = decode_army_share_code("h0p9e14_32u2x58")
    assert a.heroes[0].raw_m == "m5"
    assert b.heroes[0].raw_m is None
    assert a.identity_hash == b.identity_hash


def test_siege_extraction_preserves_origin_combined() -> None:
    home_siege = decode_army_share_code("u1x51")
    cc_siege = decode_army_share_code("i1x51")
    assert home_siege.siege[0].origin == "home"
    assert cc_siege.siege[0].origin == "clan_castle"
    # both have same typed_id but origin different


def test_unnamed_ids_are_saved_in_every_section() -> None:
    result = decode_army_share_code("u1x9991i2x9992s3x9993d4x9994h9995p9996e9997_9998")
    assert [(f.typed_id, f.quantity) for f in result.home_troops] == [("troop:9991", 1)]
    assert [(f.typed_id, f.quantity) for f in result.cc_troops] == [("troop:9992", 2)]
    assert {(f.typed_id, f.quantity, f.origin) for f in result.spells} == {
        ("spell:9993", 3, "home"),
        ("spell:9994", 4, "clan_castle"),
    }
    assert [
        (hero.hero_typed_id, hero.pet_typed_id, hero.equipment_typed_ids)
        for hero in result.heroes
    ] == [("hero:9995", "pet:9996", ("equipment:9997", "equipment:9998"))]
    assert result.identity_hash is not None


def test_empty_encoded_sections_are_structurally_unsupported() -> None:
    for code in ["u", "s", "i", "d", "h", "u2x58h", "h0p9u"]:
        result = decode_army_share_code(code)
        assert getattr(result, "category", None) == "structurally_unsupported", (
            f"expected structurally_unsupported for {code!r}, got {result}"
        )


def test_malformed_and_partial_invalidate() -> None:
    for code in ["u1x", "u1x58-", "h0p9e14_32z", "u1x58-2x", "h0p9e", "u", "h"]:
        r = decode_army_share_code(code)
        assert hasattr(r, "category")
    # empty code already tested elsewhere
    assert decode_army_share_code("").category == "empty_army_share_code"


def test_dragon_duke_revenge_deck_army_decodes_completely() -> None:
    # A current Legend army link: Dragon Duke with Revenge Deck and Fire Heart.
    result = decode_army_share_code(
        "h0p11e10_51-1p3e48_39-2p16e24_5-7p4e60_52u11x132-5x7s2x2-4x35d2x98"
    )
    assert result.identity_hash is not None
    duke = next(hero for hero in result.heroes if hero.hero_typed_id == "hero:7")
    assert {catalog.catalog_name(item) for item in duke.equipment_typed_ids} == {
        "Revenge Deck",
        "Fire Heart",
    }


def test_event_and_barracks_meteor_golems_have_different_names() -> None:
    assert catalog.catalog_name("troop:167") != catalog.catalog_name("troop:177")


def test_minion_prince_portal_pendant_army_decodes_completely() -> None:
    # A Legend army saved on 2026-10-08: Minion Prince with Portal Pendant and
    # Meteor Staff.
    result = decode_army_share_code(
        "h2m1p16e5_4-4p4e6_13-6p17e61_49-7p11e52_60i1x188-1x63-1x76d1x98-1x70"
        "u1x23-4x10-4x63-3x76-30x5-3x1-1x0s4x109-1x10-1x9-2x120-2x5-1x11"
    )
    assert result.identity_hash is not None
    prince = next(hero for hero in result.heroes if hero.hero_typed_id == "hero:6")
    assert {catalog.catalog_name(item) for item in prince.equipment_typed_ids} == {
        "Portal Pendant",
        "Meteor Staff",
    }
