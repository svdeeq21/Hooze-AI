from __future__ import annotations
import uuid
from dataclasses import dataclass
from typing import Optional

import pytest

from hooze.config_engine.activation import activate
from hooze.config_engine.schema import *
from hooze.kernel.db import PlatformDB
from hooze.kernel.events import EventBus, MemorySink
from hooze.runtime.pipeline import ConfigLoader, Pipeline, TurnOutcome
from hooze.security import sha256_hex
from hooze.settings import Settings
from tests.fakes import Clock, FakeAdapter, FakeAdapterFactory, FakeDB, FakeLLM, FakePayments, inbound

STAFF = "2348099999999"
CUSTOMER = "2348011111111"


def realestate_config(**over) -> BusinessConfig:
    props = [
        Offering(name="Maitama Terrace", description="4 bedroom terrace duplex with BQ", price_minor=150_000_000_00, category="sale",
                 details={"location": "Maitama", "price": "₦150,000,000", "bedrooms": "4"}, media=["https://cdn.test/maitama1.jpg", "https://cdn.test/maitama.mp4"]),
        Offering(name="Jabi Lakeside Flat", description="2 bedroom serviced flat near Jabi Lake", price_minor=80_000_000_00,
                 details={"location": "Jabi", "price": "₦80,000,000", "bedrooms": "2"}, media=["https://cdn.test/jabi1.jpg"]),
        Offering(name="Katampe Land", description="600sqm residential plot with C of O", price_minor=45_000_000_00,
                 details={"location": "Katampe", "price": "₦45,000,000"}),
    ]
    bc = BusinessConfig(
        identity=Identity(name="Praise Dynasty Properties", industry="real estate", website="https://praisedynasty.example", facts={"Brochure": "https://praisedynasty.example/brochure.pdf"}),
        offerings=props, faqs=[FAQ(question="Do you offer payment plans?", answer="Yes, on selected properties over 6 to 12 months.")],
        qualification_slots=[Slot(name="budget", label="Budget", type="money", ask="rough range in naira"), Slot(name="location", label="Preferred area", type="text"),
                             Slot(name="purpose", label="Purpose", type="enum", values=["living", "investment"], aliases={"living": ["live in it", "own use"]})],
        stuck_after_user_messages=6, sales_process=SalesProcess(goal="consult_and_book"), booking=BookingCfg(enabled=True, subject_label="inspection"),
        handoff=Handoff(contact_name="Sadiq", staff_phones=[STAFF]), restrictions=Restrictions(never_say=["guaranteed returns"]),
        integrations=Integrations(channel_connected=True))
    for k, v in over.items():
        setattr(bc, k, v)
    return bc


def food_config(**over) -> BusinessConfig:
    box = Offering(name="Loaded Potato Box", description="Crispy potato wedges loaded with your choices", price_minor=3_500_00, option_groups=[
        OptionGroup(name="Size", selection="single", required=True, options=[OptionChoice(name="Regular"), OptionChoice(name="Large", price_minor=1_000_00)]),
        OptionGroup(name="Toppings", selection="multiple", required=False, max_included=3, options=[OptionChoice(name=n) for n in ("Cheese", "Bacon", "Chicken", "Sausage")]),
        OptionGroup(name="Extras", selection="multiple", required=False, options=[OptionChoice(name="Extra Cheese", price_minor=300_00)])])
    drink = Offering(name="Chapman", description="Classic Nigerian cocktail (non-alcoholic)", price_minor=800_00)
    bc = BusinessConfig(identity=Identity(name="Savoury Spud"), offerings=[box, drink], sales_process=SalesProcess(goal="order"), hours={"default": ["09:00", "21:00"]},
                        handoff=Handoff(contact_name="Ngozi", staff_phones=[STAFF]), restrictions=Restrictions(never_say=["free delivery"]),
                        integrations=Integrations(payments="paystack", payments_connected=True, channel_connected=True))
    for k, v in over.items():
        setattr(bc, k, v)
    return bc


@dataclass
class World:
    db: FakeDB
    pdb: PlatformDB
    clock: Clock
    sink: MemorySink
    events: EventBus
    llm: FakeLLM
    adapter: FakeAdapter
    payments: FakePayments
    settings: Settings
    configs: ConfigLoader
    pipeline: Pipeline
    tenant: dict
    channel: dict
    transcript_text: Optional[str] = None
    _cfg: object = None

    @property
    def tid(self) -> str: return self.tenant["id"]
    @property
    def tdb(self): return self.pdb.for_tenant(self.tid)

    async def send(self, text: str, phone: str = CUSTOMER, **kw) -> TurnOutcome:
        return await self.pipeline.handle_inbound(self.channel, inbound(text, phone, **kw))

    def cap(self, name: str, cfg=None):
        """Build one capability for this tenant outside a turn (what the payments webhook / cron / admin API do)."""
        from hooze.runtime.tenant_runtime import build_tenant_runtime
        async def pay(_tid=None): return self.payments
        cfg = cfg or self._cfg
        _, caps = build_tenant_runtime(pdb=self.pdb, adapters=type("F", (), {"for_channel": lambda s_, c: self.adapter})(), events=self.events, llm=self.llm,
                                       cfg=cfg, tenant_id=self.tid, clock=self.clock, extras={"payments": pay})
        return caps[name]

    def replies(self, to: str = CUSTOMER) -> list[str]:
        return self.adapter.texts(to)

    def last_reply(self, to: str = CUSTOMER) -> str:
        return self.replies(to)[-1]


async def make_world(bc: BusinessConfig, *, tenant_name: str = "acme", db: FakeDB | None = None, clock: Clock | None = None, **settings_over) -> World:
    clock = clock or Clock()
    db = db or FakeDB(clock)
    pdb = PlatformDB(db)
    sink = MemorySink()
    events = EventBus([sink])
    llm, adapter, payments = FakeLLM(), FakeAdapter(), FakePayments()
    settings = Settings(_env_file=None, typing_delay_min=0.0, typing_delay_max=0.0, **settings_over)
    tenant = (await pdb.table("tenants").insert({"slug": tenant_name, "name": bc.identity.name, "status": "active"}).execute()).data[0]
    channel = (await pdb.table("channels").insert({"tenant_id": tenant["id"], "kind": "whatsapp_evolution", "instance_name": f"inst-{tenant_name}", "config": {"base_url": "http://evo.test"},
                                                   "secret_hash": sha256_hex("s3cret"), "status": "active"}).execute()).data[0]
    configs = ConfigLoader(pdb, ttl=0)
    async def pay(_tid=None): return payments
    pipeline = Pipeline(platform_db=pdb, llm=llm, events=events, adapters=FakeAdapterFactory(adapter), configs=configs, settings=settings, clock=clock,
                        extras_for=lambda tid: {"payments": pay}, raise_errors=True, transcriber=None)
    res = await activate(pdb, llm, tenant["id"], bc, configs=configs)
    assert res.ok, res.report.to_dict()
    llm.calls.clear()
    w = World(db, pdb, clock, sink, events, llm, adapter, payments, settings, configs, pipeline, tenant, channel)
    w._cfg = await configs.load_active(tenant["id"])
    return w


@pytest.fixture
async def re_world() -> World:
    return await make_world(realestate_config())


@pytest.fixture
async def food_world() -> World:
    return await make_world(food_config())
