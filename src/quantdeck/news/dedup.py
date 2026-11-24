"""Group near-identical headlines into clusters.

The same story usually shows up on several outlets and gets reposted on
social media. Counting each copy as separate evidence would let one story
dominate a verdict, so copies join a cluster: the cluster counts once, and
the number of distinct sources in it becomes a corroboration bonus instead.
"""

from __future__ import annotations

import re

_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{2,40}$")   # " - Reuters", " | CoinDesk"
_TOKEN = re.compile(r"[a-z0-9$]+|[一-鿿]")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "are", "as", "at", "by", "with",
         "its", "it", "from", "after", "be", "has", "have", "this", "that", "will", "says", "said"}


def tokens(title: str) -> frozenset:
    t = _SUFFIX.sub("", title.strip()).lower()
    return frozenset(w for w in _TOKEN.findall(t) if w not in _STOP)


def similarity(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    # overlap relative to the shorter title catches "X does Y" vs "X does Y, shares jump"
    return max(inter / len(a | b), inter / min(len(a), len(b)) * 0.85)


class Deduper:
    def __init__(self, threshold: float = 0.6, min_tokens: int = 4) -> None:
        self.threshold = threshold
        self.min_tokens = min_tokens
        self._clusters: list[tuple[str, frozenset]] = []
        self._index: dict[str, list[int]] = {}

    def seed(self, rows: list[tuple]) -> None:
        """rows: (id, cluster_id, title, ...) of recent items already stored."""
        for row in rows:
            self._add(row[1], tokens(row[2]))

    def _add(self, cluster_id: str, toks: frozenset) -> None:
        idx = len(self._clusters)
        self._clusters.append((cluster_id, toks))
        for t in toks:
            self._index.setdefault(t, []).append(idx)

    def assign(self, item_id: str, title: str) -> str:
        toks = tokens(title)
        if len(toks) >= self.min_tokens:
            seen: dict[int, int] = {}
            for t in toks:
                for idx in self._index.get(t, ()):
                    seen[idx] = seen.get(idx, 0) + 1
            best, best_sim = None, 0.0
            for idx, shared in seen.items():
                if shared < 2:
                    continue
                cid, other = self._clusters[idx]
                s = similarity(toks, other)
                if s > best_sim:
                    best, best_sim = cid, s
            if best is not None and best_sim >= self.threshold:
                self._add(best, toks)
                return best
        self._add(item_id, toks)
        return item_id
