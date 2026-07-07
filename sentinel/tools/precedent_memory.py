from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

try:
    from agents import function_tool
except ImportError:  # pragma: no cover
    def function_tool(func):
        return func

from sentinel.models import Case, Precedent, Verdict
from sentinel.config import DB_DIR
from sentinel.tools.audit_log import db_connection, init_db, utc_now
from sentinel.tools.policy_retrieval import TIER1_CATEGORIES


TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def case_signature(case: Case) -> str:
    return " ".join(
        [
            str(case.asset_type),
            str(case.metadata.get("expected_category", "")),
            str(case.metadata.get("synthetic_label", "")),
        ]
    )


def embed_case(case: Case) -> str:
    return json.dumps(sorted(tokenize(case_signature(case))))


def embed_vector(case: Case, dimensions: int = 32) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokenize(case_signature(case)):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = digest[0] % dimensions
        vector[index] += 1.0
    magnitude = sum(value * value for value in vector) ** 0.5
    if magnitude:
        return [value / magnitude for value in vector]
    return vector


def _similarity(left_embedding: str, right_embedding: str) -> float:
    left = set(json.loads(left_embedding))
    right = set(json.loads(right_embedding))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _chroma_path(db_path: str | Path) -> Path:
    return Path(db_path).parent / "chroma"


def _use_chroma(db_path: str | Path) -> bool:
    try:
        Path(db_path).resolve().relative_to(DB_DIR.resolve())
        return True
    except ValueError:
        return False


def _retrieve_chroma_precedents(case: Case, db_path: str | Path, limit: int) -> list[Precedent]:
    if not _use_chroma(db_path):
        return []
    try:
        import chromadb
    except ImportError:
        return []
    try:
        client = chromadb.PersistentClient(path=str(_chroma_path(db_path)))
        collection = client.get_or_create_collection("sentinel_precedents")
        result = collection.query(
            query_embeddings=[embed_vector(case)],
            n_results=limit,
            where={"category": str(case.metadata.get("expected_category", ""))},
        )
    except Exception:
        return []
    metadatas = result.get("metadatas") or [[]]
    ids = result.get("ids") or [[]]
    precedents: list[Precedent] = []
    for index, metadata in enumerate(metadatas[0]):
        precedents.append(
            Precedent(
                id=0,
                embedding=ids[0][index] if ids and ids[0] else "",
                verdict=str(metadata.get("verdict", "allow")),
                category=str(metadata.get("category", "")),
                clause=str(metadata.get("clause", "")),
                rationale=str(metadata.get("rationale", "")),
            )
        )
    return precedents


def retrieve_precedents(case: Case, db_path: str | Path, limit: int = 3) -> list[Precedent]:
    init_db(db_path)
    category = str(case.metadata.get("expected_category", ""))
    if category in TIER1_CATEGORIES:
        return []
    chroma_precedents = _retrieve_chroma_precedents(case, db_path, limit)
    if chroma_precedents:
        return chroma_precedents
    embedding = embed_case(case)
    with db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, embedding, verdict, category, clause, rationale
            FROM precedents
            WHERE category = ?
            ORDER BY id DESC
            """,
            (category,),
        ).fetchall()
    ranked = [
        (
            _similarity(embedding, row[1]),
            Precedent(
                id=row[0],
                embedding=row[1],
                verdict=row[2],
                category=row[3],
                clause=row[4],
                rationale=row[5],
            ),
        )
        for row in rows
    ]
    return [precedent for score, precedent in sorted(ranked, key=lambda item: item[0], reverse=True) if score >= 0.2][
        :limit
    ]


def write_precedent(case: Case, verdict: Verdict, db_path: str | Path) -> None:
    if verdict.category in TIER1_CATEGORIES:
        return
    if verdict.reviewer not in {"senior", "human"}:
        return
    init_db(db_path)
    with db_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO precedents (embedding, verdict, category, clause, rationale, case_signature, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                embed_case(case),
                verdict.decision,
                verdict.category,
                verdict.policy_clause,
                verdict.rationale,
                case_signature(case),
                utc_now(),
            ),
        )
        precedent_id = int(cursor.lastrowid)
    _write_chroma_precedent(case, verdict, db_path, precedent_id)


def _write_chroma_precedent(case: Case, verdict: Verdict, db_path: str | Path, precedent_id: int) -> None:
    if not _use_chroma(db_path):
        return
    try:
        import chromadb
    except ImportError:
        return
    try:
        client = chromadb.PersistentClient(path=str(_chroma_path(db_path)))
        collection = client.get_or_create_collection("sentinel_precedents")
        collection.upsert(
            ids=[f"precedent-{precedent_id}"],
            embeddings=[embed_vector(case)],
            documents=[case_signature(case)],
            metadatas=[
                {
                    "verdict": verdict.decision,
                    "category": verdict.category,
                    "clause": verdict.policy_clause,
                    "rationale": verdict.rationale,
                }
            ],
        )
    except Exception:
        return


def clear_precedents(db_path: str | Path) -> None:
    init_db(db_path)
    with db_connection(db_path) as conn:
        conn.execute("DELETE FROM precedents")
    if not _use_chroma(db_path):
        return
    try:
        import chromadb
    except ImportError:
        return
    try:
        client = chromadb.PersistentClient(path=str(_chroma_path(db_path)))
        client.delete_collection("sentinel_precedents")
    except Exception:
        return


@function_tool
def retrieve_precedents_tool(case_summary: str) -> str:
    """Placeholder SDK tool surface; local runs call retrieve_precedents with db context."""
    return f"Precedent lookup requested for: {case_summary[:120]}"
