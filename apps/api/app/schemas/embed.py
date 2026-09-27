from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator


Vec3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]


class EmbedCameraView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: Vec3
    target: Vec3


class PublicAnnotationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    body: str | None = Field(default=None, min_length=1)
    position: Vec3
    normal: Vec3 | None = None
    camera: EmbedCameraView | None = None
    order: int = Field(gt=0)
    relatedClipId: str | None = None
    relatedClipIds: list[str] = Field(default_factory=list)
    relatedPublicationIds: list[str] = Field(default_factory=list)
    relatedProjectIds: list[str] = Field(default_factory=list)
    relatedLocationIds: list[str] = Field(default_factory=list)

    @field_validator("relatedClipIds")
    @classmethod
    def _strip_clip_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            trimmed = item.strip()
            if trimmed and trimmed not in seen:
                normalized.append(trimmed)
                seen.add(trimmed)
        return normalized

    @field_validator("relatedPublicationIds", "relatedProjectIds", "relatedLocationIds")
    @classmethod
    def _strip_relation_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            trimmed = item.strip()
            if trimmed:
                normalized.append(trimmed)
        return normalized


class PublicClipV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    src: str = Field(min_length=1)
    poster: str = Field(min_length=1)
    startTime: FiniteFloat
    endTime: FiniteFloat | None
    transcript: str | None
    description: str | None = Field(default=None, min_length=1)


class PublicModelDeliveryTierV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clientCategory: Literal["standard", "constrained"]
    variant: Literal["mobile-1024", "mobile-2048", "mobile-4096", "web-8192"]


class PublicModelDeliveryCapabilitiesV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exactRequired: Literal[True] = True
    tiers: list[PublicModelDeliveryTierV1] = Field(min_length=2, max_length=2)


class PublicObjectManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifestVersion: Literal[1]
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    posterSrc: str = Field(min_length=1)
    embedUrl: str = Field(min_length=1)
    provider: Literal["Semantic"]
    initialView: EmbedCameraView
    modelSrc: str | None = Field(default=None, min_length=1)
    modelDelivery: PublicModelDeliveryCapabilitiesV1 | None = None
    annotations: list[PublicAnnotationV1] = Field(default_factory=list)
    clips: list[PublicClipV1] = Field(default_factory=list)

    @field_validator("annotations")
    @classmethod
    def _annotations_must_not_be_empty(cls, value: list[PublicAnnotationV1]) -> list[PublicAnnotationV1]:
        if not value:
            raise ValueError("annotations must contain at least one published embed annotation")
        return value
