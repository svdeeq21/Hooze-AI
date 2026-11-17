"""What actually arrives on Nigerian WhatsApp: captioned photos ("how much is this?"), disappearing-message chats
(ephemeral wrappers), view-once media. Before this round the caption was silently dropped (the bot answered
"I can't view images here" and ignored the question) and ephemeral TEXT parsed as kind "other"."""
import pytest

from hooze.channels.evolution import EvolutionAdapter
from tests.fakes import inbound

adapter = EvolutionAdapter("http://evo.invalid", "", "inst-acme")


def body(message: dict, mtype: str = "conversation", msg_id: str = "M1") -> dict:
    return {"event": "messages.upsert", "instance": "inst-acme",
            "data": {"key": {"remoteJid": "2348011111111@s.whatsapp.net", "fromMe": False, "id": msg_id},
                     "pushName": "Ada", "messageType": mtype, "message": message}}


# ── captions ──────────────────────────────────────────────────────────
def test_a_photo_with_a_caption_keeps_the_caption_as_its_text():
    m = adapter.parse(body({"imageMessage": {"caption": "how much is this one?", "url": "..."}}, mtype="imageMessage"))
    assert m.kind == "image" and m.text == "how much is this one?"


def test_a_photo_without_a_caption_still_parses_as_wordless_media():
    m = adapter.parse(body({"imageMessage": {"url": "..."}}, mtype="imageMessage"))
    assert m.kind == "image" and m.text == ""


def test_video_and_document_captions_are_read_too():
    assert adapter.parse(body({"videoMessage": {"caption": "is this the large size?"}}, mtype="videoMessage")).text == "is this the large size?"
    assert adapter.parse(body({"documentMessage": {"caption": "my payment proof"}}, mtype="documentMessage")).text == "my payment proof"


# ── disappearing-messages / view-once wrappers ────────────────────────
def test_text_inside_a_disappearing_messages_chat_is_read():
    m = adapter.parse(body({"ephemeralMessage": {"message": {"conversation": "do you deliver to Wuse?"}}}, mtype="ephemeralMessage"))
    assert m.kind == "text" and m.text == "do you deliver to Wuse?"


def test_a_view_once_photo_with_a_caption_is_read():
    m = adapter.parse(body({"viewOnceMessageV2": {"message": {"imageMessage": {"caption": "got this from your page"}}}}, mtype="viewOnceMessageV2"))
    assert m.kind == "image" and m.text == "got this from your page"


def test_nested_wrappers_unwrap_without_looping_on_junk():
    m = adapter.parse(body({"ephemeralMessage": {"message": {"viewOnceMessage": {"message": {"conversation": "hi"}}}}}, mtype="ephemeralMessage"))
    assert m.text == "hi"
    junk = adapter.parse(body({"ephemeralMessage": {"message": {"ephemeralMessage": {"message": {"ephemeralMessage": {"message": {"ephemeralMessage": {"message": {}}}}}}}}}, mtype="ephemeralMessage"))
    assert junk is not None and junk.text == ""         # bounded descent: parses (as other), never hangs


def test_plain_text_and_button_replies_are_unchanged():
    assert adapter.parse(body({"conversation": "hello"})).kind == "text"
    tap = adapter.parse(body({"buttonsResponseMessage": {"selectedDisplayText": "Pay now"}}, mtype="buttonsResponseMessage"))
    assert tap.kind == "text" and tap.text == "Pay now"


# ── through the pipeline ──────────────────────────────────────────────
async def test_a_captioned_photo_is_answered_not_dismissed(food_world):
    w = food_world
    w.llm.say("The Loaded Potato Box is ₦3,500 — want me to add one for you?", intent="question")
    out = await w.pipeline.handle_inbound(w.channel, inbound("how much is this?", kind="image"))
    assert out.status == "replied"
    assert all("can't view images" not in r for r in w.replies())
    assert "₦3,500" in w.last_reply()


async def test_a_wordless_photo_still_gets_the_canned_media_reply(food_world):
    w = food_world
    out = await w.pipeline.handle_inbound(w.channel, inbound("", kind="image"))
    assert out.status == "media" and "describe what you need" in (out.reply or "")
    assert not w.llm.calls                                             # the LLM never sees a turn it can't help with
