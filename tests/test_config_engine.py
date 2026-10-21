"""Business Configuration Engine: validate -> compile -> activate. No `if industry` anywhere."""
import copy

import pytest

from hooze.config_engine.activation import activate
from hooze.config_engine.compiler import compile_config
from hooze.config_engine.schema import *
from hooze.config_engine.validator import validate
from tests.conftest import STAFF, food_config, make_world, realestate_config


def codes(report, kind="blocking"): return {i.code for i in getattr(report, kind)}


# ── validator: the readiness checklist ─────────────────────────────────
def test_empty_config_lists_exactly_what_onboarding_must_still_collect():
    r = validate(BusinessConfig(identity=Identity(name="X"), sales_process=SalesProcess(goal="consult_and_book")))
    assert not r.ok and codes(r) == {"handoff.staff_phones", "qualification_slots", "knowledge", "booking.enabled", "integrations.channel"}
    assert r.completeness < 0.5


def test_nothing_chosen_yet_blocks_on_goal_and_name():
    assert {"identity.name", "sales_process.goal"} <= codes(validate(BusinessConfig()))


def test_complete_configs_pass_for_both_goals():
    r = validate(realestate_config())
    assert r.ok and r.completeness == 1.0, r.to_dict()
    r = validate(food_config())                                                       # fixture has no official links: exactly one warning, nothing blocking
    assert r.ok and codes(r, "warnings") == {"identity.facts"} and r.completeness == 0.93


def test_order_goal_blocks_on_unpriced_products_empty_required_groups_and_unconnected_payments():
    bc = food_config()
    bc.offerings[1].price_minor = None
    bc.offerings[0].option_groups[0].options = []
    bc.integrations.payments_connected = False
    assert {"offerings.price", "offerings.options", "integrations.payments_connected"} <= codes(validate(bc))


def test_missing_hours_and_prohibited_claims_warn_but_do_not_block():
    bc = food_config(); bc.hours = {}; bc.restrictions = Restrictions()
    r = validate(bc)
    assert r.ok and {"hours", "restrictions"} <= codes(r, "warnings")


# ── compiler ───────────────────────────────────────────────────────────
def test_compile_is_deterministic():
    assert compile_config(realestate_config()).runtime.model_dump() == compile_config(realestate_config()).runtime.model_dump()


def test_same_goal_same_machinery_industry_is_only_a_label():
    """A dental clinic and a real-estate agency are the SAME pack: zero code for a new consult_and_book vertical."""
    re_ = realestate_config()
    dental = copy.deepcopy(re_); dental.identity.industry = "dental clinic"; dental.identity.name = "Bright Smile Dental"
    a, b = compile_config(re_).runtime, compile_config(dental).runtime
    assert set(a.capabilities) == set(b.capabilities) and a.playbook == b.playbook
    relabel = copy.deepcopy(re_); relabel.identity.industry = "something else entirely"
    assert compile_config(relabel).runtime.model_dump() == a.model_dump()          # industry string changes NOTHING


def test_goals_select_different_capability_sets():
    assert set(compile_config(realestate_config()).runtime.capabilities) == {"handoff", "qualification", "knowledge", "booking", "nudges"}
    assert set(compile_config(food_config()).runtime.capabilities) == {"handoff", "ordering", "availability"}


def test_compiled_persona_facts_and_staff_numbers():
    bc = food_config(); bc.handoff.staff_phones = ["+234 809 999 9999", "0803-000-1111"]
    rt = compile_config(bc).runtime
    assert rt.staff_numbers == ["2348099999999", "2348030001111"]        # a number typed the local way (0803…) is stored in the form WhatsApp reports
    assert rt.persona.never_say == ["free delivery"] and rt.persona.facts["Opening hours"].startswith("Mon 09:00-21:00")


def test_consult_compile_derives_card_fields_from_offering_details_and_flags_video():
    rt = compile_config(realestate_config())
    fields = [f["field"] for f in rt.runtime.capabilities["knowledge"]["card_fields"]]
    assert fields == ["title", "location", "price", "bedrooms"]
    docs = {d.title: d for d in rt.knowledge_docs}
    assert {m["type"] for m in docs["Maitama Terrace"].media} == {"image", "video"} and docs["Do you offer payment plans?"].kind == "faq"
    assert rt.runtime.capabilities["nudges"]["dry_run"] is True                        # new tenants start in dry-run


def test_order_compile_builds_catalog_not_knowledge():
    c = compile_config(food_config())
    assert c.knowledge_docs == [] and [p["name"] for p in c.catalog] == ["Loaded Potato Box", "Chapman"]
    assert c.catalog[0]["groups"][1]["max_selections"] == 3


# ── activation ─────────────────────────────────────────────────────────
async def test_activation_refuses_an_invalid_config_and_writes_nothing():
    w = await make_world(realestate_config(), tenant_name="seed")
    bad = realestate_config(); bad.handoff.staff_phones = []
    t2 = (await w.pdb.table("tenants").insert({"slug": "t2", "name": "T2", "status": "onboarding"}).execute()).data[0]
    res = await activate(w.pdb, w.llm, t2["id"], bad)
    assert not res.ok and "handoff.staff_phones" in codes(res.report)
    assert w.db.rows("tenant_configs", tenant_id=t2["id"]) == [] and w.db.rows("knowledge_items", tenant_id=t2["id"]) == []


async def test_reactivation_bumps_version_archives_the_old_config_and_keeps_one_active(food_world):
    w = food_world
    bc = food_config(); bc.communication.tone = "playful and quick"
    res = await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)
    assert res.ok and res.version == 2
    assert sorted(r["status"] for r in w.db.rows("tenant_configs")) == ["active", "archived"]
    assert (await w.configs.load_active(w.tid)).persona.tone == "playful and quick"


async def test_catalog_seeding_is_idempotent_and_preserves_owner_edits(food_world):
    w = food_world
    await w.send("bacon is out", phone=STAFF)                                          # the owner marks a topping sold out
    await activate(w.pdb, w.llm, w.tid, food_config(), configs=w.configs)              # re-activation must not resurrect it or duplicate anything
    assert len(w.db.rows("products")) == 2 and len(w.db.rows("modifier_groups")) == 3
    assert next(m for m in w.db.rows("modifiers") if m["name"] == "Bacon")["available"] is False


async def test_activation_does_not_go_live_with_a_half_seeded_knowledge_base():
    """The docstring promises 'a failure midway leaves the previous config serving'. An embedding outage must not silently
    activate a consult tenant that knows nothing about its own listings."""
    w = await make_world(realestate_config(), tenant_name="seed")
    t2 = (await w.pdb.table("tenants").insert({"slug": "t2", "name": "T2", "status": "onboarding"}).execute()).data[0]
    w.llm.embed_fail = True
    res = await activate(w.pdb, w.llm, t2["id"], realestate_config(), configs=w.configs)
    assert not res.ok and res.retryable and "could not be embedded" in res.error
    assert w.db.rows("tenant_configs", tenant_id=t2["id"]) == []
    w.llm.embed_fail = False                                                          # provider recovers: the same call now succeeds
    assert (await activate(w.pdb, w.llm, t2["id"], realestate_config(), configs=w.configs)).ok
