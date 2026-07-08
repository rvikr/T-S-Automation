"""Semantic policy retrieval: the policy corpus embedded into ChromaDB.

Build/rebuild the index with ``python -m sentinel.tools.policy_index``.
``semantic_policy_search`` returns None whenever the semantic path is
unavailable (no API key, index not built, any runtime error) so callers can
fall back to keyword retrieval and offline flows never need the network.
"""

from __future__ import annotations

from typing import Any

from sentinel.config import DB_DIR, load_settings
from sentinel.tools.policy_retrieval import POLICY_CLAUSES, ROBLOX_STANDARDS_URL, PolicyClause


COLLECTION_NAME = "sentinel_policy"


def _chroma_collection():
    import chromadb

    client = chromadb.PersistentClient(path=str(DB_DIR / "chroma"))
    return client.get_or_create_collection(COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def _clause_document(clause: PolicyClause) -> str:
    return f"{clause.category}. Pillar: {clause.pillar}. Severity tier {clause.tier}. {clause.summary}"


def _embed_texts(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    settings = load_settings()
    response = OpenAI().embeddings.create(model=settings.embed_model, input=texts)
    return [item.embedding for item in response.data]


def build_policy_index() -> int:
    clauses = list(POLICY_CLAUSES.values())
    documents = [_clause_document(clause) for clause in clauses]
    embeddings = _embed_texts(documents)
    collection = _chroma_collection()
    collection.upsert(
        ids=[clause.clause_id for clause in clauses],
        embeddings=embeddings,
        documents=documents,
        metadatas=[
            {
                "category": clause.category,
                "pillar": clause.pillar,
                "tier": clause.tier,
                "clause_id": clause.clause_id,
                "summary": clause.summary,
            }
            for clause in clauses
        ],
    )
    return len(clauses)


def semantic_policy_search(query: str, limit: int = 3) -> list[dict[str, Any]] | None:
    """Top clauses by embedding similarity, or None when semantic search is unavailable."""
    settings = load_settings()
    if not settings.openai_api_key_present:
        return None
    try:
        collection = _chroma_collection()
        indexed = collection.count()
        if indexed == 0:
            return None
        embedding = _embed_texts([query])[0]
        result = collection.query(query_embeddings=[embedding], n_results=min(limit, indexed))
    except Exception:
        return None

    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    clauses: list[dict[str, Any]] = []
    for index, metadata in enumerate(metadatas):
        clauses.append(
            {
                "category": str(metadata.get("category", "")),
                "pillar": str(metadata.get("pillar", "")),
                "severity_tier": int(metadata.get("tier", 0)),
                "clause_id": str(metadata.get("clause_id", "")),
                "summary": str(metadata.get("summary", "")),
                "source_url": ROBLOX_STANDARDS_URL,
                "relevance": round(1.0 - float(distances[index]), 4) if index < len(distances) else None,
            }
        )
    return clauses or None


if __name__ == "__main__":
    count = build_policy_index()
    print(f"Indexed {count} policy clauses into '{COLLECTION_NAME}' at {DB_DIR / 'chroma'}")
