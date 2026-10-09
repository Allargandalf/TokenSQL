"""Fail-closed OpenAI-compatible client for a local Qwen service."""

from __future__ import annotations

import json
import ipaddress
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .schemas import ModelCandidate, QwenResult, QwenSelection, ReviewReason

_REASONING_EFFORTS = {"none", "low", "medium", "high"}


class QwenClientConfig(BaseModel):
    """Connection settings; localhost is the safe default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str | None = None
    model: str = "qwen3.8-flash-next-iq3_s"
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    use_json_schema: bool = True
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    allow_remote: bool = False
    max_tokens: int = Field(default=8192, ge=256, le=32768)
    reasoning_effort: str = "low"
    max_in_flight: int = Field(default=1, ge=1, le=8)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized not in _REASONING_EFFORTS:
                raise ValueError(
                    "reasoning_effort must be one of none, low, medium, high"
                )
            return normalized
        return value

    @model_validator(mode="after")
    def validate_endpoint(self) -> "QwenClientConfig":
        candidate = self.base_url.strip().rstrip("/")
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        host = parsed.hostname.casefold()
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not self.allow_remote and not loopback:
            raise ValueError("base_url must be loopback unless allow_remote=true")
        object.__setattr__(self, "base_url", candidate)
        if self.api_key is not None:
            object.__setattr__(self, "api_key", self.api_key.strip() or None)
        return self

    @classmethod
    def from_settings(cls, settings: Any) -> "QwenClientConfig":
        return cls(
            base_url=settings.qwen_base_url,
            api_key=settings.qwen_api_key,
            model=settings.qwen_model or cls().model,
            timeout_seconds=settings.qwen_timeout_seconds,
            min_confidence=settings.qwen_min_confidence,
            allow_remote=settings.qwen_allow_remote,
            max_tokens=settings.qwen_max_tokens,
            reasoning_effort=settings.qwen_reasoning_effort,
            max_in_flight=settings.qwen_max_in_flight,
        )


class _Message(BaseModel):
    # Transport envelope: unknown gateway extension fields are ignored so a
    # shared gateway can evolve its response shape; business content JSON is
    # still validated strictly by the caller-facing schemas.
    model_config = ConfigDict(extra="ignore")
    role: str | None = None
    content: str
    refusal: str | None = None
    reasoning_content: str | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int | None = None
    message: _Message
    finish_reason: str | None = None
    stop_reason: int | str | None = None
    logprobs: None = None


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_tokens_details: dict[str, Any] | None = None


class _ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    object: str | None = None
    created: int | None = None
    model: str | None = None
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None
    system_fingerprint: str | None = None
    service_tier: str | None = None
    timings: dict[str, Any] | None = None


_SYSTEM_PROMPT = """You resolve a model name only against the supplied numbered candidates.
Return JSON with exactly candidate_index, confidence, reason, and abstain.
candidate_index is the zero-based list index, never a database identifier.
Never emit SQL, database IDs, primary keys, tool calls, or additional fields.
Preserve version, date, official/self-hosted, cache, resolution, fast, and
reference qualifiers. Abstain whenever evidence is insufficient or conflicts."""


class QwenClient:
    """Small synchronous client with one request and no automatic retries."""

    def __init__(
        self,
        config: QwenClientConfig | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        use_json_schema: bool | None = None,
        min_confidence: float | None = None,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if config is not None and any(
            value is not None
            for value in (
                base_url,
                api_key,
                model,
                timeout,
                use_json_schema,
                min_confidence,
            )
        ):
            raise ValueError("pass either config or individual settings, not both")
        if config is None:
            defaults = QwenClientConfig()
            config = QwenClientConfig(
                base_url=base_url if base_url is not None else defaults.base_url,
                api_key=api_key if api_key is not None else defaults.api_key,
                model=model if model is not None else defaults.model,
                timeout_seconds=(
                    timeout if timeout is not None else defaults.timeout_seconds
                ),
                use_json_schema=(
                    use_json_schema
                    if use_json_schema is not None
                    else defaults.use_json_schema
                ),
                min_confidence=(
                    min_confidence
                    if min_confidence is not None
                    else defaults.min_confidence
                ),
            )
        self.config = config
        self._owns_client = http_client is None
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = http_client or httpx.Client(
            timeout=config.timeout_seconds,
            headers=headers,
            transport=transport,
            trust_env=False,
        )

    @classmethod
    def from_settings(
        cls, settings: Any | None = None, **kwargs: Any
    ) -> "QwenClient":
        if settings is None:
            from tokensql.config import get_settings

            settings = get_settings()
        return cls(QwenClientConfig.from_settings(settings), **kwargs)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "QwenClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _review(reason: ReviewReason, detail: str) -> QwenResult:
        return QwenResult(
            selection=None,
            review_required=True,
            review_reason=reason,
            reason=detail,
        )

    def _request_payload(
        self, query: str, candidates: Sequence[ModelCandidate]
    ) -> dict[str, Any]:
        safe_candidates = [
            {
                "candidate_index": index,
                "canonical_name": candidate.canonical_name,
                "aliases": list(candidate.aliases),
                "description": candidate.description,
            }
            for index, candidate in enumerate(candidates)
        ]
        user_content = json.dumps(
            {"query": query, "candidates": safe_candidates},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "max_tokens": 256,
        }
        if self.config.use_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "model_candidate_selection",
                    "strict": True,
                    "schema": QwenSelection.model_json_schema(),
                },
            }
        return payload

    def select_candidate(
        self,
        query: str,
        candidates: Sequence[ModelCandidate | dict[str, Any]],
    ) -> QwenResult:
        """Ask Qwen to select only a supplied list index, failing to review."""

        if not isinstance(query, str) or not query.strip():
            return self._review(ReviewReason.UNKNOWN, "model name is blank")
        try:
            validated = tuple(
                candidate
                if isinstance(candidate, ModelCandidate)
                else ModelCandidate.model_validate(candidate)
                for candidate in candidates
            )
        except (ValidationError, TypeError, ValueError) as exc:
            return self._review(
                ReviewReason.INVALID_CANDIDATE,
                f"candidate validation failed: {exc}",
            )
        if not validated:
            return self._review(ReviewReason.UNKNOWN, "no candidates were supplied")

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        try:
            response = self._client.post(
                url,
                json=self._request_payload(query, validated),
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            return self._review(ReviewReason.TIMEOUT, "Qwen request timed out")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = (
                ReviewReason.SERVER_ERROR
                if status >= 500
                else ReviewReason.CLIENT_ERROR
            )
            return self._review(reason, f"Qwen HTTP status {status}")
        except httpx.HTTPError as exc:
            return self._review(
                ReviewReason.CLIENT_ERROR,
                f"Qwen transport error: {type(exc).__name__}",
            )

        try:
            completion = _ChatCompletion.model_validate(response.json())
        except json.JSONDecodeError:
            return self._review(ReviewReason.INVALID_JSON, "Qwen returned invalid JSON")
        except (ValidationError, ValueError, TypeError) as exc:
            return self._review(
                ReviewReason.INVALID_RESPONSE,
                f"chat completion validation failed: {exc}",
            )

        try:
            raw_selection = json.loads(completion.choices[0].message.content)
        except (json.JSONDecodeError, TypeError):
            return self._review(
                ReviewReason.INVALID_JSON,
                "Qwen message content is not valid JSON",
            )
        try:
            selection = QwenSelection.model_validate(raw_selection)
        except ValidationError as exc:
            return self._review(
                ReviewReason.INVALID_RESPONSE,
                f"selection validation failed: {exc}",
            )

        if selection.abstain:
            return QwenResult(
                selection=selection,
                review_required=True,
                review_reason=ReviewReason.UNKNOWN,
                reason=selection.reason,
            )
        if selection.candidate_index is None or selection.candidate_index >= len(
            validated
        ):
            return self._review(
                ReviewReason.INVALID_CANDIDATE,
                "Qwen selected an index outside the supplied candidate set",
            )
        if selection.confidence < self.config.min_confidence:
            return QwenResult(
                selection=selection,
                review_required=True,
                review_reason=ReviewReason.LOW_CONFIDENCE,
                reason=(
                    f"Qwen confidence {selection.confidence:.3f} is below "
                    f"{self.config.min_confidence:.3f}"
                ),
            )
        return QwenResult(
            selection=selection,
            review_required=False,
            reason=selection.reason,
        )

    judge = select_candidate
