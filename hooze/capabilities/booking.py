"""Booking: viewing / appointment / inspection / consultation requests with human confirmation.

Extracted from: pd `booking.py` (create_inspection_request / confirm_inspection, dateparser
suggestion), pd `InspectionConfirmRequest` validation (audit M3), pd notifications
INSPECTION_REQUEST + BOOKING_CONFIRMED.

Integrity (audit H5): confirming/declining touches the booking AND the conversation, so it goes through
one Postgres function (`confirm_booking_tx`) — commit both or neither. Creating a booking is a single insert
(one write => nothing to get out of sync); the playbook state change is derived from the tool result.
The customer's own words are kept (`preferred_text`) next to a parsed `suggested_at` that a human confirms
— it never becomes a booking time on its own (audit M4).
"""
from __future__ import annotations
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
from pydantic import BaseModel

from hooze.capabilities.base import Capability, register
from hooze.kernel.db import first
from hooze.runtime.actions import Tool
from hooze.runtime.models import BusinessRuleError, ToolResult


class _Request(BaseModel):
    subject: str
    preferred_time: str


_TIME = re.compile(r"\d|\b(today|tomorrow|tonight|morning|afternoon|evening|noon|midday|weekend|week|month|asap|soon|whenever|any ?time|flexible|convenien\w*|"
                  r"up to you|(you're|youre|you are) free|mon(day)?|tue(s(day)?)?|wed(nesday)?|thu(r(s(day)?)?)?|fri(day)?|sat(urday)?|sun(day)?|"
                  r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)\b", re.I)


def looks_like_a_time(text: str) -> bool:
    """A real answer to "when?": a number, a day, a date or part of the day, or an explicit "whenever / any time / flexible".
    NOT "yes", "ok", "any", "n/a" or "": what the model filled in when the customer only agreed, which produced a booking with no time and
    a confirmation reading "request for X ()". Deliberately NOT dateparser: it reads "ok" as a date in 2027, "any" as tomorrow, "n/a" as next week."""
    t = (text or "").strip()
    return len(t) >= 2 and bool(_TIME.search(t))


_NON_ANSWERS = {"", "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "any", "yes please", "yes i would", "i would", "n/a", "na", "none", "null",
                "unknown", "not specified", "tbd", "?"}


def gives_a_time(text: str) -> bool:
    """False for empty values and bare affirmations. The model, pushed to "propose immediately", filled preferred_time with "" or "yes" after a
    bare "yes I would": a request with no time went to the team and the confirmation printed empty parentheses. A flexible answer such as
    "whenever you're free" IS an answer (staff will propose a slot), so this is a deny-list, not a "must look like a clock time" test."""
    return re.sub(r"[^\w\s/?]", "", (text or "")).strip().lower() not in _NON_ANSWERS


def suggest_datetime(text: str, tz: str) -> Optional[datetime]:
    try:
        import dateparser
        return dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "TIMEZONE": tz, "RETURN_AS_TIMEZONE_AWARE": True})
    except Exception:
        return None


@register
class BookingCapability(Capability):
    name = "booking"

    def tools(self) -> list[Tool]:
        label = self.config.get("subject_label", "appointment")

        async def request_booking(turn, args: _Request) -> ToolResult:
            if not gives_a_time(args.preferred_time):
                raise BusinessRuleError(f"Happy to arrange that. What day and time would suit you for the {label}?")     # ask first; nothing is booked or sent to staff
            suggested = suggest_datetime(args.preferred_time, turn.config.timezone)
            row = (await turn.db.table("bookings").insert({
                "contact_id": turn.contact["id"], "conversation_id": turn.conversation["id"], "subject": args.subject.strip()[:200],
                "preferred_text": args.preferred_time.strip()[:200], "suggested_at": suggested.isoformat() if suggested else None, "status": "pending",
            }).execute()).data[0]
            await turn.events.info("BOOKING_REQUESTED", tenant_id=turn.tenant_id, trace_id=turn.trace_id, ref_type="booking", ref_id=row["id"])
            name = turn.contact.get("display_name") or "Unknown"
            await self.ctx.outbox.notify_staff(
                f"📅 New {label} request\n{name} — +{turn.contact['phone']}\n{label.title()}: {args.subject}\nPreferred time (their words): {args.preferred_time}\n"
                + (f"Best guess: {suggested:%a %d %b, %I:%M%p} — please confirm with them; not booked yet.\n" if suggested else ""))
            return ToolResult(data={"booking_id": row["id"]},
                              message=f"Done — I've sent your {label} request for *{args.subject}* ({args.preferred_time}) to the team. "
                                      f"They'll confirm the exact slot with you shortly.")
        return [Tool("request_booking", f"submit a {label} request ONLY once the customer has chosen a subject AND given a specific day/time (if they only said yes, ask for the day and time first)", _Request, request_booking, self.name)]

    async def _finish(self, booking_id: str, status: str, when_iso: Optional[str], notes: Optional[str]) -> Optional[dict]:
        res = await self.ctx.db.rpc("confirm_booking_tx", {"p_booking_id": booking_id, "p_status": status, "p_confirmed_at": when_iso, "p_notes": notes}).execute()
        row = first(res)
        if not row:
            return None
        contact = first(await self.ctx.db.table("contacts").select("*").eq("id", row["contact_id"]).limit(1).execute())
        if contact:
            when = datetime.fromisoformat(when_iso).astimezone(ZoneInfo(self.ctx.config.timezone)) if when_iso else None     # show the customer THEIR local time
            msg = (f"✅ Confirmed: *{row['subject']}* on {when:%A %d %B at %I:%M%p}. See you then!" if status == "confirmed" and when
                   else f"Unfortunately we can't do that slot for *{row['subject']}*. Would another day work for you?")
            await self.ctx.outbox.send(contact["phone"], msg, conversation_id=row["conversation_id"])
        await self.ctx.events.info(f"BOOKING_{status.upper()}", tenant_id=self.ctx.tenant_id, ref_type="booking", ref_id=booking_id)
        return row

    async def confirm(self, booking_id: str, when_iso: str, notes: Optional[str] = None) -> Optional[dict]:
        when = datetime.fromisoformat(when_iso)     # validate BEFORE it reaches a TIMESTAMPTZ column (audit M3); raises ValueError
        if when.tzinfo is None:
            # Staff type local wall-clock time ("2026-10-03T14:00"). Postgres would read an offset-less value as UTC, i.e. an hour
            # off for a Lagos tenant, while the customer's message said 2pm. Interpret it in the TENANT's timezone instead.
            when = when.replace(tzinfo=ZoneInfo(self.ctx.config.timezone))
        return await self._finish(booking_id, "confirmed", when.isoformat(), notes)

    async def decline(self, booking_id: str, notes: Optional[str] = None) -> Optional[dict]:
        return await self._finish(booking_id, "declined", None, notes)
