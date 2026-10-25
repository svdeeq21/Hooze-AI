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
