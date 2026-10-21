"""BusinessConfig: the structured description of ONE business, produced by onboarding.

This is the "Business Configuration Engine" input. Onboarding (dashboard form, AI interview, uploaded
documents — any mix) only ever has to produce this object; nothing downstream cares HOW it was collected.
Note what is NOT here: the industry does not select code paths. `sales_process.goal` does
("order" | "consult_and_book"), which is why a dental clinic and a real-estate agency can share one pack.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class OptionChoice(BaseModel):
    name: str
    price_minor: int = 0

class OptionGroup(BaseModel):
    name: str
    selection: Literal["single", "multiple"] = "single"
    required: bool = False
    max_included: Optional[int] = None
    options: list[OptionChoice] = Field(default_factory=list)

class Offering(BaseModel):
    name: str
    description: str = ""
    price_minor: Optional[int] = None
    price_note: str = ""                                   # e.g. "from", "on request"
    category: str = ""
    url: str = ""
    media: list[str] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)  # free-form facts shown on the card (location, size, title, ...)
    option_groups: list[OptionGroup] = Field(default_factory=list)

class FAQ(BaseModel):
    question: str
    answer: str

class Slot(BaseModel):
    name: str
    label: str = ""
    type: Literal["text", "number", "money", "enum", "bool"] = "text"
    values: list[str] = Field(default_factory=list)
    aliases: dict[str, list[str]] = Field(default_factory=dict)   # canonical value -> other words customers use
    required: bool = True
    ask: str = ""

class Identity(BaseModel):
    name: str = ""
    industry: str = ""            # descriptive only — never selects behaviour
    description: str = ""
    website: str = ""
    address: str = ""
    languages: list[str] = Field(default_factory=lambda: ["English"])
    timezone: str = "Africa/Lagos"
    currency: str = "NGN"
    facts: dict[str, str] = Field(default_factory=dict)   # official links / handles / emails the bot may quote

class SalesProcess(BaseModel):
    goal: Optional[Literal["order", "consult_and_book"]] = None
    notes: str = ""

class BookingCfg(BaseModel):
    enabled: bool = False
    subject_label: str = "appointment"

class Handoff(BaseModel):
    contact_name: str = ""
    staff_phones: list[str] = Field(default_factory=list)
    human_request_patterns: list[str] = Field(default_factory=list)

class Communication(BaseModel):
    tone: str = "warm, brief and professional"
    greeting: str = ""
    rules: list[str] = Field(default_factory=list)

class Restrictions(BaseModel):
    never_say: list[str] = Field(default_factory=list)
    prohibited_claims: list[str] = Field(default_factory=list)

class Integrations(BaseModel):
    payments: Optional[str] = None            # "paystack"
    payments_connected: bool = False
    channel_connected: bool = False

class BusinessConfig(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    offerings: list[Offering] = Field(default_factory=list)
    faqs: list[FAQ] = Field(default_factory=list)
    qualification_slots: list[Slot] = Field(default_factory=list)
    stuck_after_user_messages: int = 14
    sales_process: SalesProcess = Field(default_factory=SalesProcess)
    booking: BookingCfg = Field(default_factory=BookingCfg)
    hours: dict[str, Optional[list[str]]] = Field(default_factory=dict)   # {"default": ["09:00","18:00"], "6": null}
    handoff: Handoff = Field(default_factory=Handoff)
    communication: Communication = Field(default_factory=Communication)
    restrictions: Restrictions = Field(default_factory=Restrictions)
    integrations: Integrations = Field(default_factory=Integrations)
