"""Dedupe primitives — state-machine.md §1.1, data-model.md §2.1,
agent-contracts.md §3.1. No DB access here (main.py owns all DB I/O,
matching clarification.py's separation) — just the hash and the
embedding call.

Verified empirically before writing this (`/tmp` scratch test, matching
the project's established pattern): text-embedding-004 works at both
regional (`us-central1`) and the `global` Vertex AI endpoint — unlike
`gemini-3.5-flash`, which 404s everywhere except `global` (found in step
4). Reuses whatever `GOOGLE_CLOUD_LOCATION` resolver-svc is already
deployed with; no new env var needed for this.
"""

import hashlib
import os
from dataclasses import dataclass

from google import genai

EMBEDDING_MODEL = "text-embedding-004"
DUPLICATE_THRESHOLD = 0.92  # state-machine.md §1.1
THREAD_ATTACH_THRESHOLD = 0.82  # state-machine.md §1.1

_client = genai.Client(
    vertexai=True,
    project=os.environ.get("GCP_PROJECT_ID"),
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
)


def compute_dedupe_hash(title: str, summary: str) -> str:
    """sha256(lower(trim(title)) || '|' || lower(trim(summary))) — data-model.md §2's DDL."""
    normalized = f"{title.strip().lower()}|{summary.strip().lower()}"
    return hashlib.sha256(normalized.encode()).hexdigest()


def embed(title: str, summary: str) -> list[float]:
    """Embed `title + summary` per state-machine.md §1.1 point 1 — newline-joined,
    the doc doesn't specify an exact concatenation format."""
    response = _client.models.embed_content(
        model=EMBEDDING_MODEL, contents=[f"{title}\n{summary}"]
    )
    return response.embeddings[0].values


def vector_literal(embedding: list[float]) -> str:
    """pgvector's text input format for a `vector` column — a plain string cast
    with `::vector` server-side, no `pgvector` python package/adapter needed
    for a write-only, single-column use like this one."""
    return "[" + ",".join(repr(v) for v in embedding) + "]"


@dataclass
class DedupeResult:
    """At most one of duplicate_item_id / thread_attach_item_id is ever set —
    state-machine.md §1.1's thresholds are mutually exclusive bands."""

    duplicate_item_id: object | None = None
    duplicate_title: str | None = None
    thread_attach_item_id: object | None = None
    thread_attach_title: str | None = None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure Python equivalent of pgvector's `1 - (a <=> b)` — kept separate
    from the real SQL cosine search so the threshold decision below is
    unit-testable against fixture vectors, no DB or embedding call needed."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b)


def classify_match(similarity: float, match_type: str, match_id, match_title: str) -> DedupeResult:
    """state-machine.md §1.1 points 2-4: the threshold decision, isolated
    from the DB query that finds the candidate in the first place."""
    if similarity >= DUPLICATE_THRESHOLD:
        return DedupeResult(duplicate_item_id=match_id, duplicate_title=match_title)
    if similarity >= THREAD_ATTACH_THRESHOLD and match_type == "latent":
        return DedupeResult(thread_attach_item_id=match_id, thread_attach_title=match_title)
    return DedupeResult()
