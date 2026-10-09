# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

import pytest

from huiyiku.domain.chunks import cosine, rrf_fuse, tokenize
from huiyiku.domain.citations import normalize_for_match, verify_citation
from huiyiku.domain.report_schema import validate_report
from huiyiku.llm.client import estimate_tokens, map_reduce_groups


def test_citation_ignores_whitespace_and_punct() -> None:
    corpus = ["我们 下周，上线。"]
    assert verify_citation("我们下周，上线。", corpus)
    assert normalize_for_match("A，B") == "A,B"
    assert normalize_for_match("Hello，world") == normalize_for_match("Hello, world")


def test_rrf_and_cosine() -> None:
    fused = rrf_fuse([1, 2, 3], [3, 1], k=60, limit=3)
    assert fused[0] in {1, 3}
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        cosine([1.0, 0.0], [1.0])


def test_tokenize_not_empty() -> None:
    assert "会议" in tokenize("这次会议很重要")


def test_map_reduce_covers_all() -> None:
    items = ["a" * 100 for _ in range(20)]
    groups = map_reduce_groups(items, context_length=200)
    flat = [x for g in groups for x in g]
    assert len(flat) == 20
    assert groups


def test_report_schema() -> None:
    with pytest.raises(ValueError):
        validate_report({})
    ok = {
        "summary": "s",
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "risks": [],
        "open_questions": [],
        "participants": [],
    }
    assert validate_report(ok)["summary"] == "s"


def test_estimate_tokens() -> None:
    assert estimate_tokens("你好世界") >= 1
    assert estimate_tokens("") == 0
