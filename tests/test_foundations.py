"""Tenant isolation, fail-closed security, channel parsing, playbook engine."""
import pytest

from hooze.channels.evolution import EvolutionAdapter
from hooze.kernel.db import GLOBAL_TABLES, PlatformDB, TenantDB, TenantScopeError
from hooze.runtime.playbook import Condition as C, PlaybookSpec, StateSpec as S, Transition as T, TurnFacts
from hooze.security import Vault, hmac_sha512_hex, secrets_match, sha256_hex
from tests.fakes import Clock, FakeDB


# ── TenantDB: isolation is structural ──────────────────────────────────
async def test_tenantdb_injects_tenant_id_and_isolates_reads():
    db = FakeDB(Clock())
    a, b = TenantDB(db, "tenant-a"), TenantDB(db, "tenant-b")
    await a.table("contacts").insert({"phone": "234801"}).execute()
    await b.table("contacts").insert({"phone": "234801"}).execute()       # same phone, different tenant: allowed
    assert {r["tenant_id"] for r in db.rows("contacts")} == {"tenant-a", "tenant-b"}
    assert len((await a.table("contacts").select("*").execute()).data) == 1
    assert (await a.table("contacts").select("*").execute()).data[0]["tenant_id"] == "tenant-a"


async def test_tenantdb_cannot_update_or_delete_another_tenants_rows():
    db = FakeDB(Clock())
    a, b = TenantDB(db, "tenant-a"), TenantDB(db, "tenant-b")
    row = (await b.table("contacts").insert({"phone": "1", "display_name": "B's contact"}).execute()).data[0]
    await a.table("contacts").update({"display_name": "hacked"}).eq("id", row["id"]).execute()
    await a.table("contacts").delete().eq("id", row["id"]).execute()
    survivor = db.rows("contacts")[0]
    assert survivor["display_name"] == "B's contact"          # a forgotten filter is impossible: the scope is not optional


async def test_tenantdb_refuses_global_tables_and_reparenting():
    t = TenantDB(FakeDB(Clock()), "tenant-a")
    for name in GLOBAL_TABLES:
        with pytest.raises(TenantScopeError):
            t.table(name)
    with pytest.raises(TenantScopeError):
        t.table("contacts").update({"tenant_id": "tenant-b"})
    with pytest.raises(TenantScopeError):
        TenantDB(FakeDB(Clock()), "")


async def test_tenantdb_rpc_always_carries_tenant_id():
    seen = {}
    class Client:
        def rpc(self, name, params): seen.update(params); return None
    TenantDB(Client(), "tenant-a").rpc("match_knowledge", {"p_count": 3})
    assert seen["p_tenant_id"] == "tenant-a" and seen["p_count"] == 3


async def test_db_unique_constraints_the_app_relies_on_are_enforced_by_the_fake():
    db = FakeDB(Clock()); t = TenantDB(db, "t")
    await t.table("orders").insert({"contact_id": "c1", "status": "CART"}).execute()
    with pytest.raises(Exception):                                       # one CART per contact (partial unique index)
        await t.table("orders").insert({"contact_id": "c1", "status": "CART"}).execute()
    await t.table("orders").insert({"contact_id": "c1", "status": "PAID"}).execute()   # other statuses are unconstrained


# ── security primitives fail closed ────────────────────────────────────
def test_secrets_match_fails_closed():
    h = sha256_hex("right")
    assert secrets_match("right", h)
    assert not secrets_match("wrong", h)
    assert not secrets_match(None, h) and not secrets_match("", h)
    assert not secrets_match("right", None) and not secrets_match("right", "")   # no stored secret => never authorised (audit C1)


def test_vault_roundtrip_and_ciphertext_differs():
    v = Vault(Vault.generate_key())
    token = v.encrypt("sk_live_abc123")
    assert token != "sk_live_abc123" and v.decrypt(token) == "sk_live_abc123"


def test_hmac_sha512_matches_paystack_construction():
    import hashlib, hmac
    body = b'{"event":"charge.success"}'
    assert hmac_sha512_hex("k", body) == hmac.new(b"k", body, hashlib.sha512).hexdigest()


# ── Evolution adapter: verification + parsing ──────────────────────────
CH = {"instance_name": "inst-1", "secret_hash": sha256_hex("s3cret")}
ADAPTER = EvolutionAdapter("http://evo", "k", "inst-1")


def _body(message: dict, mtype="conversation", jid="2348011111111@s.whatsapp.net", from_me=False, event="messages.upsert", instance="inst-1"):
    return {"event": event, "instance": instance, "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": "ABC"}, "pushName": "Ada", "messageType": mtype, "message": message}}


def test_webhook_verification_is_fail_closed():
    b = _body({"conversation": "hi"})
    assert ADAPTER.verify(provided_secret="s3cret", body=b, channel=CH)
    assert not ADAPTER.verify(provided_secret=None, body=b, channel=CH)
    assert not ADAPTER.verify(provided_secret="nope", body=b, channel=CH)
    assert not ADAPTER.verify(provided_secret="s3cret", body=b, channel={**CH, "secret_hash": None})     # channel with no secret: reject even if header present
    assert not ADAPTER.verify(provided_secret="s3cret", body=_body({"conversation": "hi"}, instance="other"), channel=CH)   # wrong instance


@pytest.mark.parametrize("message,mtype,text,kind", [
    ({"conversation": "hello"}, "conversation", "hello", "text"),
    ({"extendedTextMessage": {"text": "long one"}}, "extendedTextMessage", "long one", "text"),
    ({"buttonsResponseMessage": {"selectedDisplayText": "Yes please", "selectedButtonId": "b1"}}, "buttonsResponseMessage", "Yes please", "text"),
    ({"listResponseMessage": {"title": "Large", "singleSelectReply": {"selectedRowId": "r1"}}}, "listResponseMessage", "Large", "text"),
    ({"audioMessage": {"seconds": 5}}, "audioMessage", "", "audio"),
    ({"imageMessage": {"caption": ""}}, "imageMessage", "", "image"),
    ({"stickerMessage": {}}, "stickerMessage", "", "sticker"),
    ({"reactionMessage": {"text": "👍"}}, "reactionMessage", "", "reaction"),
])
def test_parse_message_kinds(message, mtype, text, kind):
    m = ADAPTER.parse(_body(message, mtype))
    assert (m.text, m.kind, m.phone, m.push_name) == (text, kind, "2348011111111", "Ada")


def test_parse_ignores_other_events_and_flags_groups_and_own_messages():
    assert ADAPTER.parse(_body({"conversation": "x"}, event="connection.update")) is None
    assert ADAPTER.parse({"event": "messages.upsert", "data": {}}) is None
    assert ADAPTER.parse(_body({"conversation": "x"}, jid="1203630@g.us")).is_group
    assert ADAPTER.parse(_body({"conversation": "x"}, from_me=True)).from_me


# ── playbook engine ────────────────────────────────────────────────────
def _pb(transitions, states=("A", "B", "C")):
    return PlaybookSpec(initial="A", states=[S(name=n) for n in states], transitions=transitions)


def test_shipped_packs_are_structurally_sound():
    from hooze.packs import get_pack
    for goal in ("consult_and_book", "order"):
        assert get_pack(goal).playbook.validate_spec() == []


def test_validate_spec_catches_broken_playbooks():
    assert any("unknown state" in p for p in _pb([T(**{"from": ["A"], "to": "Z", "when": C(always=True)})]).validate_spec())
    assert any("unreachable" in p and "'C'" in p for p in _pb([T(**{"from": ["A"], "to": "B", "when": C(always=True)})]).validate_spec())
    assert any("not defined" in p for p in PlaybookSpec(initial="X", states=[S(name="A")]).validate_spec())


def test_first_match_wins_and_a_self_edge_holds_the_state():
    pb = _pb([T(**{"from": ["A"], "to": "A", "when": C(intent_in=["objection"])}), T(**{"from": ["A"], "to": "B", "when": C(always=True)})])
    assert pb.next_state("A", TurnFacts(intent="objection")) == "A"       # hold
    assert pb.next_state("A", TurnFacts(intent="question")) == "B"


def test_empty_condition_never_matches_and_terminal_is_sticky():
    pb = PlaybookSpec(initial="A", states=[S(name="A"), S(name="B", terminal=True)],
                      transitions=[T(**{"from": ["A"], "to": "B", "when": C()}), T(**{"from": ["*"], "to": "A", "when": C(always=True)})])
    assert pb.next_state("A", TurnFacts()) == "A"                          # C() matched nothing; the always-edge ran instead
    assert pb.next_state("B", TurnFacts()) == "B"


def test_pd_rule_never_back_to_discovery_after_presenting_is_graph_structure():
    """pd's 'DISCOVERY never comes after PITCH' was a comment + careful code. Here it is the absence of an edge."""
    from hooze.packs.consult_and_book import PLAYBOOK
    later = {"PRESENTING", "REFINING", "CONFIRMING", "BOOKING"}
    for t in PLAYBOOK.transitions:
        if t.to == "DISCOVERY":
            assert not (set(t.from_) & later) and "*" not in t.from_
