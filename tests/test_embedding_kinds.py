"""Documents being indexed and customer questions must be embedded in their own modes; changing the mode must refresh stored vectors."""
from types import SimpleNamespace

from hooze.capabilities.knowledge import KnowledgeDoc, sync_documents
from hooze.llm.gateway import GeminiProvider


def gemini(captured):
    class Models:
        async def embed_content(self, *, model, contents, config):
            captured.append(config.task_type)
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1, 0.2])])
    p = GeminiProvider("k", "gemini-x", "embed-x")
    p._client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    return p


async def test_gemini_uses_the_document_type_for_indexing_and_the_query_type_for_questions():
    got = []
    p = gemini(got)
    await p.embed("a menu item", 2, "document")
    await p.embed("do you deliver?", 2, "query")
    await p.embed("default is a question", 2)
    assert got == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY", "RETRIEVAL_QUERY"]


async def test_activation_indexes_knowledge_as_documents(food_world):
    w = food_world
    kinds = []
    real = w.llm.embed
    async def spy(tenant_id, text, **kw):
        kinds.append(kw.get("kind")); return await real(tenant_id, text, **kw)
    w.llm.embed = spy
    from hooze.config_engine.activation import activate
    from tests.conftest import food_config
    from hooze.config_engine.schema import FAQ
    bc = food_config(); bc.faqs.append(FAQ(question="Do you open on Sundays?", answer="Yes, from noon."))
    assert (await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)).ok
    assert kinds and set(kinds) == {"document"}


async def test_a_new_embedding_recipe_makes_stored_documents_stale_so_they_are_re_embedded(food_world, monkeypatch):
    db, embedded = food_world.tdb, []
    async def emb(t): embedded.append(t); return [0.0]
    docs = [KnowledgeDoc(external_id="faq:1", kind="faq", title="Q", content="A")]
    await sync_documents(db, emb, docs, source="config")
    await sync_documents(db, emb, docs, source="config")
    assert len(embedded) == 1                                                          # unchanged => skipped
    monkeypatch.setattr("hooze.capabilities.knowledge.EMBED_RECIPE", "document-embeddings-v3")
    stats = await sync_documents(db, emb, docs, source="config")
    assert len(embedded) == 2 and stats["updated"] == 1                                # recipe changed => refreshed once


# ── model families: gemini-embedding-2 (task in the text) vs gemini-embedding-001 (task_type) ──────────
def capturing(model):
    got = []
    class Models:
        async def embed_content(self, *, model, contents, config):
            got.append({"contents": contents, "task_type": getattr(config, "task_type", None), "dims": config.output_dimensionality})
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0])])         # length 5 on purpose: must come back unit length
    p = GeminiProvider("k", "gemini-x", model)
    p._client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    return p, got


async def test_gemini_embedding_2_never_sends_task_type_and_writes_the_task_into_the_text():
    p, got = capturing("gemini-embedding-2")
    q = await p.embed("do you deliver to Ikeja?", 768, "query")
    await p.embed("Delivery\nWe deliver to Ikeja and Yaba.", 768, "document")
    await p.embed("no title here", 768, "document")
    assert [g["task_type"] for g in got] == [None, None, None]                         # the API rejects the parameter for this model
    assert got[0]["contents"] == "task: search result | query: do you deliver to Ikeja?"
    assert got[1]["contents"] == "title: Delivery | text: We deliver to Ikeja and Yaba."
    assert got[2]["contents"] == "title: none | text: no title here"
    assert all(g["dims"] == 768 for g in got)
    assert abs(sum(x * x for x in q) - 1.0) < 1e-6                                     # shortened vectors are normalised


async def test_the_retired_preview_name_and_models_prefix_are_treated_as_the_new_family_not_as_task_type_models():
    for name in ("models/gemini-embedding-2", "gemini-embedding-2-preview", "models/gemini-embedding-2-preview"):
        p, got = capturing(name)
        await p.embed("hi", 768, "query")
        assert got[0]["task_type"] is None and got[0]["contents"].startswith("task: search result"), name


async def test_gemini_embedding_001_still_uses_task_type_and_plain_text():
    p, got = capturing("gemini-embedding-001")
    await p.embed("a question", 768, "query"); await p.embed("a doc", 768, "document")
    assert [(g["task_type"], g["contents"]) for g in got] == [("RETRIEVAL_QUERY", "a question"), ("RETRIEVAL_DOCUMENT", "a doc")]


# ── switching model refreshes stored vectors ───────────────────────────
async def test_the_embedding_model_is_part_of_the_recipe_so_a_model_change_re_embeds_everything(food_world):
    from hooze.capabilities.knowledge import KnowledgeDoc, sync_documents
    from hooze.llm.gateway import LLMGateway
    db, embedded = food_world.tdb, []
    async def emb(t): embedded.append(t); return [0.0]
    docs = [KnowledgeDoc(external_id="faq:r", kind="faq", title="Q", content="A")]
    old = LLMGateway([GeminiProvider("k", "m", "gemini-embedding-2-preview")]).embedding_recipe()
    new = LLMGateway([GeminiProvider("k", "m", "gemini-embedding-2")]).embedding_recipe()
    assert old != new and "gemini-embedding-2" in new and ":768" in new
    await sync_documents(db, emb, docs, source="config", recipe=old)
    assert (await sync_documents(db, emb, docs, source="config", recipe=old))["unchanged"] == 1
    assert (await sync_documents(db, emb, docs, source="config", recipe=new))["updated"] == 1 and len(embedded) == 2


# ── failures name their real cause ─────────────────────────────────────
async def test_a_retired_model_error_is_explained_and_the_real_reason_survives_the_cooldown():
    import pytest
    from hooze.llm.gateway import LLMGateway, LLMUnavailable
    class Dead:
        name, model, embedding_model = "gemini", "m", "gemini-embedding-2-preview"
        async def embed(self, text, dims, kind="query"): raise RuntimeError("404 NOT_FOUND: models/gemini-embedding-2-preview is not found")
    gw = LLMGateway([Dead()], provider_cooldown=30)
    with pytest.raises(LLMUnavailable) as first:
        await gw.embed("t", "hello", fail_fast=True)
    assert "NOT_FOUND" in str(first.value) and "EMBEDDING_MODEL=gemini-embedding-2" in str(first.value)
    with pytest.raises(LLMUnavailable) as second:                                        # provider is now being skipped: the cause must not vanish
        await gw.embed("t", "hello", fail_fast=True)
    assert "NOT_FOUND" in str(second.value) and "cooling down" not in str(second.value)


# ── re-index endpoint ──────────────────────────────────────────────────
async def test_reindex_re_embeds_with_the_current_model_only_when_it_changed(re_world):
    from tests.test_api import client, container
    from tests.test_operator_api import P
    w = re_world
    w.llm.embedding_recipe = lambda: "gemini:model-A:768"
    from hooze.config_engine.activation import activate
    from tests.conftest import realestate_config
    assert (await activate(w.pdb, w.llm, w.tid, realestate_config(), configs=w.configs)).ok
    n_docs = len(w.db.rows("knowledge_items"))
    calls = []
    real = w.llm.embed
    async def spy(tid, text, **kw): calls.append(kw.get("kind")); return await real(tid, text, **kw)
    w.llm.embed = spy
    async with client(container(w)) as c:
        r1 = await c.post("/v1/platform/embeddings/reindex", headers=P)
        assert r1.status_code == 200 and r1.json()["tenants"][w.tid]["unchanged"] == n_docs and calls == []          # nothing stale
        w.llm.embedding_recipe = lambda: "gemini:model-B:768"                                                          # the model was switched
        r2 = await c.post(f"/v1/platform/embeddings/reindex?tenant_id={w.tid}", headers=P)
    out = r2.json()["tenants"][w.tid]
    assert out["status"] == "ok" and out["updated"] == n_docs and set(calls) == {"document"} and len(calls) == n_docs


async def test_reindex_reports_a_failing_embedding_model_instead_of_pretending(re_world):
    from tests.test_api import client, container
    from tests.test_operator_api import P
    from hooze.llm.gateway import LLMUnavailable
    w = re_world
    w.llm.embedding_recipe = lambda: "gemini:model-B:768"
    async def broken(*a, **k): raise LLMUnavailable("no embedding provider available: 404 NOT_FOUND")
    w.llm.embed = broken
    async with client(container(w)) as c:
        r = await c.post("/v1/platform/embeddings/reindex", headers=P)
    assert r.json()["tenants"][w.tid]["status"] == "failed" and r.json()["tenants"][w.tid]["failed"] >= 1


async def test_a_non_platform_key_cannot_trigger_a_reindex(re_world):
    from tests.test_api import client, container
    from tests.test_operator_api import tenant_key
    w = re_world
    async with client(container(w)) as c:
        assert (await c.post("/v1/platform/embeddings/reindex", headers={"x-api-key": await tenant_key(w)})).status_code == 403
