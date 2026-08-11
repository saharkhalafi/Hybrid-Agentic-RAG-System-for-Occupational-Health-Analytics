"""In-memory lexical index for Persian semantic chunks (BM25-style)."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import DocumentChunk
from retrieval.semantic_retrieval import SEMANTIC_LANGUAGE, SEMANTIC_SOURCE_TYPE, SEMANTIC_VALIDATION_STATUS

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_TEST_CHUNK_PREFIX = "test_persian_"


def _tokenize(text: str) -> list[str]:
    t = text.lower().translate(_PERSIAN_DIGITS)
    t = re.sub(r"[^\w\s\u0600-\u06FF]", " ", t, flags=re.UNICODE)
    return [w for w in t.split() if len(w) > 1]


@dataclass
class LexicalDocument:
    chunk_id: str
    content: str
    metadata: dict[str, Any]
    tf: Counter[str] = field(default_factory=Counter)
    length: int = 0


class LexicalIndex:
    """Simple BM25 lexical index over production semantic chunks."""

    def __init__(self) -> None:
        self.docs: list[LexicalDocument] = []
        self.df: Counter[str] = Counter()
        self.avg_dl: float = 0.0
        self._built = False

    def build_from_session(self, session: Session) -> int:
        rows = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
                DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
                DocumentChunk.language == SEMANTIC_LANGUAGE,
            )
        ).all()
        self.docs = []
        self.df = Counter()
        for chunk in rows:
            cid = chunk.chunk_id or ""
            if cid.startswith(_TEST_CHUNK_PREFIX):
                continue
            content = chunk.content or ""
            # Contextual prefix for retrieval (does not mutate stored content)
            prefix_parts = [p for p in [chunk.section_title, chunk.topic] if p]
            enriched = f"{' | '.join(prefix_parts)}\n{content}" if prefix_parts else content
            tokens = _tokenize(enriched)
            tf = Counter(tokens)
            doc = LexicalDocument(
                chunk_id=cid,
                content=content,
                metadata={
                    "chunk_id": cid,
                    "page_number": chunk.page_number,
                    "printed_page_number": chunk.printed_page_number,
                    "section_title": chunk.section_title,
                    "topic": chunk.topic,
                    "section_id": chunk.section_id,
                    "enriched_content": enriched,
                },
                tf=tf,
                length=len(tokens),
            )
            self.docs.append(doc)
            for term in set(tokens):
                self.df[term] += 1
        n = len(self.docs)
        self.avg_dl = sum(d.length for d in self.docs) / n if n else 0.0
        self._built = n > 0
        return n

    def search(self, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
        if not self._built:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        n = len(self.docs)
        k1, b = 1.5, 0.75
        scores: list[tuple[float, LexicalDocument]] = []
        for doc in self.docs:
            score = 0.0
            for term in q_tokens:
                if term not in doc.tf:
                    continue
                df = self.df.get(term, 0)
                idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
                tf = doc.tf[term]
                denom = tf + k1 * (1 - b + b * doc.length / max(self.avg_dl, 1))
                score += idf * (tf * (k1 + 1)) / denom
            if score > 0:
                scores.append((score, doc))
        scores.sort(key=lambda x: x[0], reverse=True)
        out: list[dict[str, Any]] = []
        for score, doc in scores[:limit]:
            out.append({
                "chunk_id": doc.chunk_id,
                "content": doc.content,
                "score": score,
                "retrieval_method": "lexical_bm25",
                **doc.metadata,
            })
        return out
