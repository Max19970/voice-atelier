"""Request bodies (pydantic v2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=200)


class SegmentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = Field(default=None, max_length=64)
    text: str = Field(default="", max_length=10000)
    voice_id: str | None = Field(default=None, max_length=64)
    mode: str = "clone"
    model_id: str | None = Field(default=None, max_length=64)
    language: str = Field(default="ru", max_length=16)
    instruction: str = Field(default="", max_length=1500)
    rate: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    pause_ms: int = Field(default=300, ge=0, le=5000)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    selected_take_id: str | None = Field(default=None, max_length=64)


class ProjectSave(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = Field(default=None, max_length=200)
    segments: list[SegmentIn] = Field(default_factory=list, max_length=200)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segment_ids: list[str] | None = Field(default=None, max_length=200)
    force: bool = False


class SelectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segment_id: str = Field(max_length=64)
    take_id: str = Field(max_length=64)


class VoicePatch(BaseModel):
    """Only the human-readable fields; audio/reference/consent are immutable."""

    model_config = ConfigDict(extra="ignore")
    name: str | None = Field(default=None, max_length=120)
    transcript: str | None = Field(default=None, max_length=5000)


class DesignRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = Field(default=None, max_length=120)
    text: str = Field(min_length=1, max_length=10000)
    instruction: str = Field(min_length=1, max_length=1500)
    language: str = Field(default="ru", max_length=16)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    rate: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    save_as_voice: bool = False
    consent: bool = False


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    format: Literal["wav", "zip"] = "wav"
    normalize: bool = False
    allow_stale: bool = False


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    accept_license: bool = False
