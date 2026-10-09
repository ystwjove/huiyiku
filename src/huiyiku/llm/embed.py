# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import struct

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from huiyiku.config import RuntimeContext
from huiyiku.db.models import Meeting, MeetingChunk, ModelRegistry
from huiyiku.llm.client import ChatError


def pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def embeddings_url(endpoint: str) -> str:
    base = (endpoint or "").rstrip("/")
    if base.endswith("/embeddings"):
        return base
    if base.endswith("/v1"):
        return base + "/embeddings"
    return base + "/v1/embeddings"


def embed_text(
    runtime: RuntimeContext,
    model: ModelRegistry,
    text: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[float]:
    if not runtime.settings.allow_cloud:
        raise ChatError("allow_cloud is false")
    key = runtime.secrets.embedding_api_key or runtime.secrets.llm_api_key
    if not key:
        raise ChatError("embedding API key missing")
    url = embeddings_url(model.endpoint or "")
    try:
        with httpx.Client(timeout=120.0, transport=transport) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model.model_name, "input": text},
            )
        if resp.status_code in {401, 403}:
            raise ChatError(resp.text, status_code=resp.status_code, retryable=False)
        if resp.status_code >= 400:
            raise ChatError(resp.text, status_code=resp.status_code)
        vec = resp.json()["data"][0]["embedding"]
    except ChatError:
        raise
    except httpx.HTTPError as exc:
        raise ChatError(str(exc), retryable=True) from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChatError(f"invalid embedding response: {exc}", retryable=True) from exc
    if model.embedding_dim and len(vec) != model.embedding_dim:
        raise ChatError(f"embedding dim {len(vec)} != {model.embedding_dim}")
    return vec


def embed_texts_batch(
    runtime: RuntimeContext,
    model: ModelRegistry,
    texts: list[str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[list[float]]:
    """批量 embedding：一次请求编码多条文本，按返回的 index 字段对齐顺序。"""
    if not texts:
        return []
    if not runtime.settings.allow_cloud:
        raise ChatError("allow_cloud is false")
    key = runtime.secrets.embedding_api_key or runtime.secrets.llm_api_key
    if not key:
        raise ChatError("embedding API key missing")
    url = embeddings_url(model.endpoint or "")
    try:
        with httpx.Client(timeout=120.0, transport=transport) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model.model_name, "input": texts},
            )
        if resp.status_code in {401, 403}:
            raise ChatError(resp.text, status_code=resp.status_code, retryable=False)
        if resp.status_code >= 400:
            raise ChatError(resp.text, status_code=resp.status_code)
        data = resp.json()["data"]
    except ChatError:
        raise
    except httpx.HTTPError as exc:
        raise ChatError(str(exc), retryable=True) from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ChatError(f"invalid embedding response: {exc}", retryable=True) from exc
    if not isinstance(data, list) or len(data) != len(texts):
        raise ChatError(f"embedding batch size mismatch: {len(data) if isinstance(data, list) else '?'} != {len(texts)}")
    pairs: list[tuple[int, list[float]]] = []
    for i, row in enumerate(data):
        idx = row.get("index", i) if isinstance(row, dict) else i
        pairs.append((int(idx), row["embedding"]))
    if len({idx for idx, _ in pairs}) != len(pairs):
        # index 重复会导致顺序对齐错位，宁可报错也不能静默错配
        raise ChatError("embedding response has duplicate index values")
    pairs.sort(key=lambda p: p[0])
    vecs = [v for _, v in pairs]
    if model.embedding_dim and any(len(v) != model.embedding_dim for v in vecs):
        raise ChatError(f"embedding dim {[len(v) for v in vecs if len(v) != model.embedding_dim][0]} != {model.embedding_dim}")
    return vecs


def unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def embed_active_chunks(
    session: Session,
    runtime: RuntimeContext,
    meeting: Meeting,
    progress,
    cancel_check=None,
) -> None:
    model = session.scalar(
        select(ModelRegistry).where(
            ModelRegistry.task == "embedding",
            ModelRegistry.is_default == 1,
            ModelRegistry.status == "available",
        )
    )
    if model is None:
        return
    if not runtime.settings.allow_cloud:
        raise ChatError("allow_cloud is false")
    key = runtime.secrets.embedding_api_key or runtime.secrets.llm_api_key
    if not key:
        raise ChatError("embedding API key missing")
    chunks = list(
        session.scalars(
            select(MeetingChunk).where(
                MeetingChunk.meeting_id == meeting.id,
                MeetingChunk.is_active == 1,
                MeetingChunk.embedding.is_(None),
            )
        )
    )
    if not chunks:
        return
    dim = model.embedding_dim
    for i, chunk in enumerate(chunks):
        if cancel_check and cancel_check():
            raise ChatError("cancelled")
        progress({"stage": "embed", "i": i, "n": len(chunks)})
        vec = embed_text(runtime, model, chunk.content)
        if dim and len(vec) != dim:
            raise ChatError(f"embedding dim {len(vec)} != {dim}")
        if dim is None:
            dim = len(vec)
            model.embedding_dim = dim
        chunk.embedding = pack_embedding(vec)
        chunk.embedding_model = f"{model.provider}:{model.model_name}"
        chunk.embedding_dim = dim
