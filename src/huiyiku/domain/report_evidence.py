# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

"""报告条目到转写原文的本地溯源。

模型在报告里自报的 segment_ids / time_ms 从未与真实转写核对过（map-reduce
长会议下尤其不可靠），这里改为本地把每条报告内容对回真实转写段，给出三档
结论与证据段：quoted（原句照搬）/ paraphrase（释义有据）/ unsupported
（录音中未找到对应原文，即 AI 补充）。模型给的 id 只作候选排序线索，
不单独作为依据。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from huiyiku.domain.chunks import tokenize
from huiyiku.domain.citations import normalize_for_match

# 释义档的调参入口：单段内容词覆盖率 / 时间窗覆盖率 / 最少命中内容词数 / 证据段上限。
# 阈值按 80 分钟真实会议（1806 段、145 条报告条目）校准：单段 0.5、窗口 0.55；
# 试过给短条目放宽到 0.4，真实数据一条没多救回、反而让泛词组合误报，故不做
PARAPHRASE_COVERAGE = 0.5
WINDOW_COVERAGE = 0.55
PARAPHRASE_MIN_HITS = 2
MAX_EVIDENCE = 3

# 向量兜底：词法三档都未命中（unsupported）的条目，用条目向量与转写块向量
# 的余弦相似度再救一次；≥ 该阈值取 top1-2 改判 paraphrase（时间近似匹配，
# 证据项带 vector_matched 标记）。与 WINDOW_COVERAGE 同源校准。
VECTOR_MIN_SCORE = 0.55

# 多字虚词：与"长度>=2"的过滤互补，命中率统计里剔除，否则虚词处处命中、
# 阈值失去意义
_STOPWORDS = frozenset(
    {
        "我们", "你们", "他们", "她们", "它们", "自己", "大家",
        "什么", "怎么", "哪个", "哪些", "这个", "那个", "这些", "那些",
        "这样", "那样", "其中", "以下", "以上",
        "因为", "所以", "如果", "虽然", "但是", "然后", "并且", "而且",
        "或者", "还是", "只是", "就是", "不是", "没有", "一个", "一些",
        "关于", "通过", "对于", "以及", "目前", "现在", "已经", "基本",
        "可以", "应该", "可能", "需要", "进行", "一下", "的话", "时候",
    }
)

# 报告里逐条溯源的分节；action_items 取 title 字段
_EVIDENCE_SECTIONS = ("key_points", "decisions", "action_items", "risks", "open_questions")


def content_tokens(text: str) -> list[str]:
    """分词后保留内容词：长度 >=2 且不在停用词表。"""
    return [
        t for t in tokenize(text or "").split() if len(t) >= 2 and t not in _STOPWORDS
    ]


def segment_dicts_from_rows(segs, speakers: dict[int, Any]) -> list[dict[str, Any]]:
    """把 TranscriptSegment 行 + 说话人映射转成索引输入的段字典。"""
    out: list[dict[str, Any]] = []
    for seg in segs:
        sp = speakers.get(seg.speaker_id) if seg.speaker_id else None
        name = (sp.display_name if sp else None) or (sp.speaker_label if sp else "?")
        out.append(
            {
                "id": seg.id,
                "start_ms": seg.start_ms,
                "end_ms": seg.end_ms,
                "speaker_id": seg.speaker_id,
                "speaker_name": name,
                "text": seg.normalized_text or seg.text,
            }
        )
    return out


class EvidenceIndex:
    """一次构建、多次查询的转写段索引。

    构建成本 O(总字数)；quoted 检查走子串搜索，释义打分走 token 倒排，
    几十条报告条目在一两千段上是毫秒到百毫秒级。报告条目常把散在相邻
    多段的内容浓缩成一句，所以除单段外再做时间窗（默认 120 秒，与转写
    块粒度一致）联合匹配。
    """

    def __init__(self, segments: list[dict[str, Any]], *, window_ms: int = 120_000):
        # segments: [{id, start_ms, end_ms, speaker_id, speaker_name, text}]
        self._segs = list(segments)
        self._norm = [normalize_for_match(str(s.get("text") or "")) for s in self._segs]
        self._tokens = [set(tokenize(str(s.get("text") or "")).split()) for s in self._segs]
        self._postings: dict[str, list[int]] = {}
        for i, toks in enumerate(self._tokens):
            for tok in toks:
                self._postings.setdefault(tok, []).append(i)
        self._window_ms = window_ms

    def _windows(self) -> list[tuple[int, frozenset[str]]]:
        """以每段为起点、按时间窗合并后续段的 token（懒构建 + 缓存）。"""
        cached = getattr(self, "_win_cache", None)
        if cached is not None:
            return cached
        out: list[tuple[int, frozenset[str]]] = []
        n = len(self._segs)
        for i in range(n):
            start = int(self._segs[i].get("start_ms") or 0)
            toks: set[str] = set()
            j = i
            while j < n and int(self._segs[j].get("start_ms") or 0) < start + self._window_ms:
                toks |= self._tokens[j]
                j += 1
            out.append((i, frozenset(toks)))
        self._win_cache = out
        return out

    def resolve(
        self, text: str, *, hint_segment_ids: list[int] | None = None
    ) -> dict[str, Any]:
        needle = normalize_for_match(text or "")
        if not needle:
            return {"status": "unsupported", "evidence": []}
        # 模型自报的 segment_ids 可能是任意垃圾（实测见过 "1303-1307" 区间串），
        # 只把能转成 int 的当线索
        hints: set[int] = set()
        for x in hint_segment_ids or []:
            try:
                hints.add(int(x))
            except (TypeError, ValueError):
                continue

        matched = [i for i, hay in enumerate(self._norm) if needle in hay]
        if matched:
            evidence = self._take(matched, hints, key=lambda i: 0)
            return {"status": "quoted", "evidence": evidence}

        item_tokens = set(content_tokens(text))
        if not item_tokens:
            return {"status": "unsupported", "evidence": []}
        need = len(item_tokens)
        counts: Counter[int] = Counter()
        for tok in item_tokens:
            for i in self._postings.get(tok, ()):
                counts[i] += 1
        candidates = [
            i
            for i, hits in counts.items()
            if hits >= PARAPHRASE_MIN_HITS and hits / need >= PARAPHRASE_COVERAGE
        ]
        if candidates:
            evidence = self._take(candidates, hints, key=lambda i: counts[i])
            return {"status": "paraphrase", "evidence": evidence}

        # 时间窗联合匹配：条目把相邻几段的内容浓缩成一句时，单段覆盖不了
        best_i, best_cov = None, 0.0
        for i, toks in self._windows():
            cov = len(item_tokens & toks) / need
            if cov > best_cov:
                best_i, best_cov = i, cov
        if best_i is not None and best_cov >= WINDOW_COVERAGE:
            start = int(self._segs[best_i].get("start_ms") or 0)
            members = []
            j = best_i
            while j < len(self._segs) and int(self._segs[j].get("start_ms") or 0) < (
                start + self._window_ms
            ):
                hits = len(item_tokens & self._tokens[j])
                if hits > 0:
                    members.append((hits, j))
                j += 1
            if not members:
                members = [(0, best_i)]
            members.sort(key=lambda hj: (-hj[0], hj[1]))
            picked = [j for _, j in members[:MAX_EVIDENCE]]
            evidence = self._take(picked, hints, key=lambda i: 0)
            return {"status": "paraphrase", "evidence": evidence}
        return {"status": "unsupported", "evidence": []}

    def _take(
        self, indices: list[int], hints: set[int], *, key: Any
    ) -> list[dict[str, Any]]:
        # 模型给的 id 只在同分候选里优先：命中提示 id 的排前面，
        # 其余按命中词数 / 出现顺序排，最多取 MAX_EVIDENCE 段
        ordered = sorted(
            indices,
            key=lambda i: (
                self._segs[i].get("id") in hints,
                key(i),
                -int(self._segs[i].get("start_ms") or 0),
            ),
            reverse=True,
        )
        out = []
        for i in ordered[:MAX_EVIDENCE]:
            seg = self._segs[i]
            out.append(
                {
                    "segment_id": seg.get("id"),
                    "start_ms": seg.get("start_ms"),
                    "end_ms": seg.get("end_ms"),
                    "speaker_id": seg.get("speaker_id"),
                    "speaker_name": seg.get("speaker_name"),
                    "text": seg.get("text"),
                }
            )
        return out


def resolve_report_evidence(
    data: dict[str, Any],
    index: EvidenceIndex,
    *,
    vector_fallback: Any = None,
) -> dict[str, int]:
    """就地给报告 content 的各条目加 evidence / evidence_status，返回三档计数。

    vector_fallback：可选的 callable(texts: list[str]) -> list[list[dict]]。
    词法三档判定后，仅对 unsupported 条目做**一次**批量向量调用（避免逐条
    网络往返）；候选段按相似度排序，score >= VECTOR_MIN_SCORE 的 top1-2
    改判 paraphrase，证据项标 vector_matched: True。
    """
    counts = {"quoted": 0, "paraphrase": 0, "unsupported": 0}
    pending: list[tuple[dict[str, Any], str]] = []
    for key in _EVIDENCE_SECTIONS:
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or item.get("title") or "")
            result = index.resolve(text, hint_segment_ids=item.get("segment_ids"))
            item["evidence"] = result["evidence"]
            item["evidence_status"] = result["status"]
            counts[result["status"]] += 1
            if result["status"] == "unsupported" and text:
                pending.append((item, text))
    if vector_fallback is not None and pending:
        try:
            batch = vector_fallback([text for _, text in pending])
        except Exception:
            batch = [[] for _ in pending]
        for (item, _), cands in zip(pending, batch, strict=False):
            hits = sorted(
                (c for c in cands or [] if float(c.get("score") or 0) >= VECTOR_MIN_SCORE),
                key=lambda c: -float(c.get("score") or 0),
            )[:2]
            if not hits:
                continue
            item["evidence"] = [{**c, "vector_matched": True} for c in hits]
            item["evidence_status"] = "paraphrase"
            counts["unsupported"] -= 1
            counts["paraphrase"] += 1
    return counts
