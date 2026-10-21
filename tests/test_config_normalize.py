"""The normalizer repairs near-miss configs, reports every repair, and never invents business facts."""
import json
import pathlib

import pytest

from hooze.config_engine.normalize import ConfigProblem, normalize_raw, parse_config
from hooze.config_engine.validator import validate

FIX = pathlib.Path(__file__).parent / "fixtures" / "broken_7_trays.json"


def test_the_config_that_crashed_the_setup_tab_now_loads():
    bc, fixes = parse_config(json.loads(FIX.read_text()))
    assert [s.name for s in bc.qualification_slots][:3] == ["customer_name", "customer_intent", "food_interest"]
    assert all(not s.required for s in bc.qualification_slots)            # converted strings must not block the conversation
    assert bc.identity.facts == {"products": "Food"}                      # lists joined, empties dropped
    assert bc.offerings[0].media == [] and bc.offerings[0].option_groups == [] and bc.offerings[0].price_minor is None
    assert any("qualification_slots[0]" in f for f in fixes) and any("identity.facts" in f for f in fixes)


def test_it_does_not_invent_facts_so_the_readiness_check_still_asks():
    bc, _ = parse_config(json.loads(FIX.read_text()))
    codes = {i.code for i in validate(bc).blocking}
    assert "handoff.staff_phones" in codes                                # phone numbers are never made up


def test_price_in_naira_becomes_kobo_and_is_reported():
    clean, fixes = normalize_raw({"offerings": [{"name": "Jollof", "price": "2,500"}]})
    assert clean["offerings"][0]["price_minor"] == 250000
    assert any("kobo" in f for f in fixes)


def test_invalid_goal_is_cleared_not_guessed():
    clean, fixes = normalize_raw({"sales_process": {"goal": "sell"}})
    assert clean["sales_process"]["goal"] is None and any("goal" in f for f in fixes)


def test_enum_without_choices_falls_back_to_text_and_duplicates_are_dropped():
    clean, _ = normalize_raw({"qualification_slots": [{"name": "Type", "type": "enum"}, {"name": "type"}]})
    assert len(clean["qualification_slots"]) == 1 and clean["qualification_slots"][0]["type"] == "text"


def test_hours_pair_is_wrapped():
    clean, _ = normalize_raw({"hours": ["09:00", "18:00"]})
    assert clean["hours"] == {"default": ["09:00", "18:00"]}


def test_a_valid_config_passes_through_unchanged_with_no_fixes():
    bc, fixes = parse_config(json.loads(FIX.read_text()))
    again, fixes2 = parse_config(json.loads(bc.model_dump_json()))
    assert fixes2 == [] and again == bc


def test_non_object_is_a_readable_error():
    with pytest.raises(ConfigProblem) as e:
        parse_config(["not", "an", "object"])
    assert "JSON object" in e.value.problems[0]
