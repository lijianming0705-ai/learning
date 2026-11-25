from __future__ import annotations

import re

_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_TOKEN_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+|[a-z0-9]+(?:[._-][a-z0-9]+)*")


def extract_hybrid_tokens(text: str, include_trigrams: bool = True) -> list[str]:
    """Extract multilingual tokens for both CJK and Latin scripts.

    - CJK runs are split into unigrams, overlapping bigrams, and optional trigrams.
    - Latin/numeric words keep whole words plus 3-char subword n-grams for fuzzy matching.
    """
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = []
    for run in _TOKEN_RUN_RE.findall(lowered):
        if _CJK_CHAR_RE.match(run[0]):
            n = len(run)
            for i in range(n):
                tokens.append(run[i])
                if i + 1 < n:
                    tokens.append(run[i : i + 2])
                if include_trigrams and i + 2 < n:
                    tokens.append(run[i : i + 3])
        else:
            tokens.append(run)
            if len(run) >= 4:
                for i in range(len(run) - 2):
                    tokens.append(f"~{run[i : i + 3]}")
    return tokens


def fts_tokenize(text: str) -> str:
    """Space-delimited token stream safe for SQLite FTS5 indexing & querying."""
    tokens = extract_hybrid_tokens(text, include_trigrams=False)
    # Strip subword markers from FTS query/index so FTS stays clean and fast
    cleaned = [t for t in tokens if not t.startswith("~") and t.strip()]
    return " ".join(cleaned)


def fts_query_expression(query: str) -> str:
    """Build a safe OR-combined FTS5 MATCH expression from user query."""
    tokens = [t for t in extract_hybrid_tokens(query, include_trigrams=False) if not t.startswith("~")]
    # Deduplicate while preserving order, prioritize longer tokens first (bigrams & words)
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in sorted(tokens, key=len, reverse=True):
        safe = tok.replace('"', "")
        if safe and safe not in seen:
            seen.add(safe)
            ordered.append(f'"{safe}"')
        if len(ordered) >= 24:
            break
    return " OR ".join(ordered)
