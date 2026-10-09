# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import unicodedata

_PUNCT = {
    "，": ",",
    "。": ".",
    "！": "!",
    "？": "?",
    "；": ";",
    "：": ":",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
}


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for src, dst in _PUNCT.items():
        text = text.replace(src, dst)
    text = re.sub(r"\s+", "", text)
    return text


def verify_citation(quote: str, corpus_texts: list[str]) -> bool:
    needle = normalize_for_match(quote)
    if not needle:
        return False
    return any(needle in normalize_for_match(t) for t in corpus_texts)
