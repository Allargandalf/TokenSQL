"""Strict data contracts for model matching and local LLM decisions."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    """Base contract: unrecognised fields are always a validation error."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MatchStage(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    QWEN = "qwen"
    REVIEW = "review"


class ReviewReason(str, Enum):
    AMBIGUOUS = "ambiguous"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    INVALID_JSON = "invalid_json"
    INVALID_RESPONSE = "invalid_response"
    INVALID_CANDIDATE = "invalid_candidate"
    QUALIFIER_CONFLICT = "qualifier_conflict"
    CLIENT_ERROR = "client_error"


class ModelCandidate(_StrictModel):
    """A safe, presentation-only candidate.

    Database identifiers and SQL are intentionally absent. Because extra fields
    are forbidden, database_id, primary_key, or sql can never enter an LLM
    request through this type.
    """

    canonical_name: str = Field(min_length=1, max_length=512)
    aliases: tuple[str, ...] = ()
    description: str | None = Field(default=None, max_length=1_000)

    @field_validator("canonical_name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("canonical_name cannot be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("aliases cannot contain blank values")
            if value not in cleaned:
                cleaned.append(value)
        return tuple(cleaned)


class QwenSelection(_StrictModel):
    """The only shape an LLM is permitted to return."""

    candidate_index: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=1_000)
    abstain: bool = False

    @model_validator(mode="after")
    def _consistent_abstention(self) -> QwenSelection:
        if self.abstain and self.candidate_index is not None:
            raise ValueError("an abstaining decision cannot select a candidate")
        if not self.abstain and self.candidate_index is None:
            raise ValueError("a non-abstaining decision must select a candidate")
        return self


class QwenResult(_StrictModel):
    """Validated, fail-closed result returned by the HTTP client."""

    selection: QwenSelection | None = None
    review_required: bool
    reason: str = Field(min_length=1, max_length=2_000)
    review_reason: ReviewReason | None = None

    @model_validator(mode="after")
    def _consistent_result(self) -> QwenResult:
        if self.review_required:
            if self.review_reason is None:
                raise ValueError("review results require review_reason")
        else:
            if self.selection is None or self.selection.abstain:
                raise ValueError("approved results require a non-abstaining selection")
            if self.review_reason is not None:
                raise ValueError("approved results cannot include review_reason")
        return self


class MatchResult(_StrictModel):
    """Outcome of deterministic and optional LLM matching."""

    stage: MatchStage
    candidate: ModelCandidate | None = None
    candidate_index: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2_000)
    review_required: bool
    review_reason: ReviewReason | None = None

    @model_validator(mode="after")
    def _consistent_result(self) -> MatchResult:
        if self.review_required:
            if self.stage is not MatchStage.REVIEW:
                raise ValueError("review_required results must use the review stage")
            if self.review_reason is None:
                raise ValueError("review_required results require review_reason")
            if self.candidate is not None or self.candidate_index is not None:
                raise ValueError("review results cannot approve a candidate")
        else:
            if self.stage is MatchStage.REVIEW:
                raise ValueError("approved results cannot use the review stage")
            if self.candidate is None or self.candidate_index is None:
                raise ValueError("approved results require a candidate and index")
            if self.review_reason is not None:
                raise ValueError("approved results cannot include review_reason")
        return self
