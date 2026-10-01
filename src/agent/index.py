"""In-memory TF-IDF retrieval index.

This is deliberately a small, deterministic, dependency-free stand-in for a
real vector index. It keeps the LlamaIndex-style pipeline (ingest -> index ->
retrieve with scores) so the retrieval stage can be evaluated offline and
later swapped for embeddings without touching the agent loop.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from agent.documents import Document

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class ScoredDocument:
    document: Document
    score: float


class SearchIndex:
    """TF-IDF cosine-similarity index over ingested documents."""

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._term_counts: dict[str, Counter[str]] = {}
        self._doc_freq: Counter[str] = Counter()

    def ingest(self, documents: list[Document]) -> int:
        """Index documents; re-ingesting a doc_id replaces it. Returns count."""
        for doc in documents:
            self._docs[doc.doc_id] = doc
            tokens = tokenize(doc.text)
            self._term_counts[doc.doc_id] = Counter(tokens)
            for term in set(tokens):
                self._doc_freq[term] += 1
        return len(self._docs)

    def __len__(self) -> int:
        return len(self._docs)

    def _idf(self, term: str) -> float:
        n = max(len(self._docs), 1)
        return math.log((1 + n) / (1 + self._doc_freq.get(term, 0))) + 1.0

    def retrieve(self, query: str, top_k: int = 5) -> list[ScoredDocument]:
        """Return the top_k documents ranked by TF-IDF cosine similarity."""
        query_tokens = tokenize(query)
        if not query_tokens or not self._docs:
            return []
        query_weights: dict[str, float] = {}
        for term, count in Counter(query_tokens).items():
            query_weights[term] = (1 + math.log(count)) * self._idf(term)
        query_norm = math.sqrt(sum(w * w for w in query_weights.values())) or 1.0

        scored: list[ScoredDocument] = []
        for doc_id, term_counts in self._term_counts.items():
            doc_score = 0.0
            for term, weight in query_weights.items():
                tf = term_counts.get(term, 0)
                if tf:
                    doc_score += weight * (1 + math.log(tf)) * self._idf(term)
            if doc_score > 0:
                doc_norm = math.sqrt(
                    sum((1 + math.log(c)) ** 2 * self._idf(t) ** 2 for t, c in term_counts.items())
                ) or 1.0
                scored.append(ScoredDocument(document=self._docs[doc_id], score=doc_score / (query_norm * doc_norm)))
        scored.sort(key=lambda sd: sd.score, reverse=True)
        return scored[:top_k]
