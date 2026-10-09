# Copyright 2026 会议库 contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from sqlalchemy import (
    BLOB,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class Person(Base):
    __tablename__ = "persons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    aliases_json: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"))
    title: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    original_path: Mapped[str | None] = mapped_column(Text)
    wav_path: Mapped[str | None] = mapped_column(Text)
    upload_audio_path: Mapped[str | None] = mapped_column(Text)
    waveform_path: Mapped[str | None] = mapped_column(Text)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    media_checksum: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(Text, default="zh")
    status: Mapped[str] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "person_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"))
    person_id: Mapped[int] = mapped_column(Integer, ForeignKey("persons.id"))
    source: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class SpeakerTurn(Base):
    __tablename__ = "speaker_turns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"))
    speaker_label: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class MeetingSpeaker(Base):
    __tablename__ = "meeting_speakers"
    __table_args__ = (UniqueConstraint("meeting_id", "speaker_label"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"))
    speaker_label: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    person_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("persons.id"))
    status: Mapped[str] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[str] = mapped_column(Text)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"))
    speaker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("meeting_speakers.id"))
    evidence_turn_ids_json: Mapped[str | None] = mapped_column(Text)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    asr_provider: Mapped[str | None] = mapped_column(Text)
    asr_model: Mapped[str | None] = mapped_column(Text)
    asr_model_version: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(Text, default="auto")
    updated_at: Mapped[str] = mapped_column(Text)


class SpeakerStat(Base):
    __tablename__ = "speaker_stats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"))
    speaker_id: Mapped[int] = mapped_column(Integer, ForeignKey("meeting_speakers.id"))
    speech_ms: Mapped[int] = mapped_column(Integer)
    segment_count: Mapped[int] = mapped_column(Integer)
    speech_ratio: Mapped[float] = mapped_column(Float)
    timeline_json: Mapped[str | None] = mapped_column(Text)
    metric_version: Mapped[str] = mapped_column(Text, default="v1")
    computed_at: Mapped[str] = mapped_column(Text)


class MeetingChunk(Base):
    __tablename__ = "meeting_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"))
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"))
    speaker_id: Mapped[int | None] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    chunk_type: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    tokenized_content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes | None] = mapped_column(BLOB)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    source_report_version_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(Text)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id"), unique=True)
    current_version_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class ReportVersion(Base):
    __tablename__ = "report_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id"))
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    content_json: Mapped[str] = mapped_column(Text)
    content_md: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    stale_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class ActionItem(Base):
    __tablename__ = "action_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"))
    source_meeting_id: Mapped[int | None] = mapped_column(Integer)
    source_report_version_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    owner_person_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(Text)
    user_edited: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(Text)


class ActionItemEvent(Base):
    __tablename__ = "action_item_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("action_items.id"))
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    meeting_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)


class QaMessage(Base):
    __tablename__ = "qa_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text)
    scope_json: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    citations_json: Mapped[str | None] = mapped_column(Text)
    retrieval_json: Mapped[str | None] = mapped_column(Text)
    model_json: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("meetings.id"))
    type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    progress_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String, unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    heartbeat_at: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)


class Glossary(Base):
    __tablename__ = "glossaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("groups.id"))
    term: Mapped[str] = mapped_column(Text)
    replacement: Mapped[str] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, default=1)


class ModelRegistry(Base):
    __tablename__ = "model_registry"
    __table_args__ = (UniqueConstraint("task", "provider", "model_name", "model_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    execution: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str | None] = mapped_column(Text)
    secret_ref: Mapped[str | None] = mapped_column(Text)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    license: Mapped[str | None] = mapped_column(Text)
    is_gated: Mapped[int] = mapped_column(Integer, default=0)
    terms_accepted: Mapped[int] = mapped_column(Integer, default=0)
    usage_class: Mapped[str | None] = mapped_column(Text)
    capabilities_json: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(Text, default="pending")
    status: Mapped[str] = mapped_column(Text, default="untested")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class GroupDigest(Base):
    __tablename__ = "group_digests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"))
    period_start: Mapped[str | None] = mapped_column(Text)
    period_end: Mapped[str | None] = mapped_column(Text)
    content_json: Mapped[str] = mapped_column(Text)
    content_md: Mapped[str | None] = mapped_column(Text)
    source_meeting_ids_json: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
