# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from huiyiku_asr.transcribe import maybe_has_speaker_labels, parse_funasr_result


def test_parse_sentence_info_with_spk() -> None:
    result = [
        {
            "sentence_info": [
                {"start": 0, "end": 1000, "text": "你好", "spk": 0},
                {"start": 1000, "end": 2000, "text": "世界", "spk": 1},
            ]
        }
    ]
    segs = parse_funasr_result(result)
    assert segs[0]["speaker_label"] == "SPEAKER_00"
    assert segs[1]["speaker_label"] == "SPEAKER_01"
    assert maybe_has_speaker_labels(segs)


def test_parse_without_spk_is_null() -> None:
    result = [{"text": "你好", "timestamp": [[0, 100], [100, 200]]}]
    segs = parse_funasr_result(result)
    assert segs[0]["speaker_label"] is None
    assert segs[0]["start_ms"] == 0
    assert segs[0]["end_ms"] == 200
    assert not maybe_has_speaker_labels(segs)
