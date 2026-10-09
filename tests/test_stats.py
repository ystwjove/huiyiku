# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from huiyiku.domain.stats import compute_speaker_stats


def test_ratio_excludes_merged_and_invalid() -> None:
    speakers = [
        {"id": 1, "status": "confirmed"},
        {"id": 2, "status": "confirmed"},
        {"id": 3, "status": "merged"},
        {"id": 4, "status": "invalid"},
    ]
    segments = [
        {"speaker_id": 1, "start_ms": 0, "end_ms": 1000},
        {"speaker_id": 2, "start_ms": 1000, "end_ms": 3000},
        {"speaker_id": 3, "start_ms": 0, "end_ms": 5000},
        {"speaker_id": 4, "start_ms": 0, "end_ms": 9000},
    ]
    rows = {r["speaker_id"]: r for r in compute_speaker_stats(segments, speakers)}
    assert 3 not in rows and 4 not in rows
    assert rows[1]["speech_ms"] == 1000
    assert rows[2]["speech_ms"] == 2000
    assert rows[1]["speech_ratio"] == 1 / 3
    assert rows[2]["speech_ratio"] == 2 / 3
    assert rows[1]["metric_version"] == "v1"


def test_overlap_counted_by_assignment_only() -> None:
    speakers = [{"id": 1, "status": "confirmed"}, {"id": 2, "status": "confirmed"}]
    segments = [
        {"speaker_id": 1, "start_ms": 0, "end_ms": 1000},
        {"speaker_id": 2, "start_ms": 500, "end_ms": 1500},
    ]
    rows = {r["speaker_id"]: r for r in compute_speaker_stats(segments, speakers)}
    assert rows[1]["speech_ms"] == 1000
    assert rows[2]["speech_ms"] == 1000
    assert abs(rows[1]["speech_ratio"] - 0.5) < 1e-9
