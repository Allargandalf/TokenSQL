"""Deterministic-first model matching with an optional Qwen tie-breaker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any, Protocol

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - package dependency, defensive fallback
    fuzz = None

from tokensql.llm.schemas import (
    MatchResult,
    MatchStage,
    ModelCandidate,
    QwenResult,
    ReviewReason,
)

from .normalize import NameQualifiers, extract_qualifiers, normalize_model_name


class CandidateSelector(Protocol):
    def select_candidate(
        self, query: str, candidates: Sequence[ModelCandidate]
    ) -> QwenResult: ...


def _coerce_candidate(value: ModelCandidate | str | Mapping[str, Any]) -> ModelCandidate:
    if isinstance(value, ModelCandidate):
        return value
    if isinstance(value, str):
        return ModelCandidate(canonical_name=value)
    return ModelCandidate.model_validate(value)


def _combined_qualifiers(candidate: ModelCandidate) -> NameQualifiers:
    qualifiers = [
        extract_qualifiers(name)
        for name in (candidate.canonical_name, *candidate.aliases)
    ]
    return NameQualifiers(
        versions=frozenset().union(*(item.versions for item in qualifiers)),
        dates=frozenset().union(*(item.dates for item in qualifiers)),
        resolutions=frozenset().union(*(item.resolutions for item in qualifiers)),
        sizes=frozenset().union(*(item.sizes for item in qualifiers)),
        flags=frozenset().union(*(item.flags for item in qualifiers)),
    )


def _qualifier_check(
    query: NameQualifiers, candidate: NameQualifiers
) -> tuple[bool, float, str | None]:
    """Return compatibility, fuzzy-score penalty, and conflict explanation."""

    categorical = (
        ("version", query.versions, candidate.versions),
        ("date", query.dates, candidate.dates),
        ("resolution", query.resolutions, candidate.resolutions),
        ("size", query.sizes, candidate.sizes),
    )
    penalty = 0.0
    for label, query_values, candidate_values in categorical:
        if query_values and candidate_values and query_values.isdisjoint(
            candidate_values
        ):
            return (
                False,
                100.0,
                f"{label} conflict: {sorted(query_values)} vs "
                f"{sorted(candidate_values)}",
            )
        if query_values and not candidate_values:
            return False, 100.0, f"candidate loses explicit {label} qualifier"
        if candidate_values and not query_values:
            penalty += 12.0

    if (
        "official" in query.flags
        and "self_hosted" in candidate.flags
        or "self_hosted" in query.flags
        and "official" in candidate.flags
    ):
        return False, 100.0, "official/self-hosted qualifier conflict"

    missing_flags = query.flags - candidate.flags
    if missing_flags:
        return (
            False,
            100.0,
            f"candidate loses explicit qualifiers: {sorted(missing_flags)}",
        )
    penalty += 8.0 * len(candidate.flags - query.flags)
    return True, penalty, None


def _similarity(left: str, right: str) -> float:
    if fuzz is not None:
        return float(fuzz.WRatio(left, right))
    return SequenceMatcher(a=left, b=right).ratio() * 100.0


class ModelMatcher:
    """Exact aliases, qualifier-aware fuzzy matching, then optional Qwen."""

    def __init__(
        self,
        candidates: Sequence[ModelCandidate | str | Mapping[str, Any]],
        *,
        qwen_client: CandidateSelector | None = None,
        llm_client: CandidateSelector | None = None,
        fuzzy_threshold: float = 88.0,
        ambiguity_margin: float = 5.0,
    ) -> None:
        if qwen_client is not None and llm_client is not None:
            raise ValueError("pass qwen_client or llm_client, not both")
        if not 0.0 <= fuzzy_threshold <= 100.0:
            raise ValueError("fuzzy_threshold must be between 0 and 100")
        if ambiguity_margin < 0.0:
            raise ValueError("ambiguity_margin cannot be negative")
        self.candidates = tuple(_coerce_candidate(value) for value in candidates)
        self.qwen_client = qwen_client or llm_client
        self.fuzzy_threshold = float(fuzzy_threshold)
        self.ambiguity_margin = float(ambiguity_margin)
        self._candidate_qualifiers = tuple(
            _combined_qualifiers(candidate) for candidate in self.candidates
        )

    @staticmethod
    def _approved(
        stage: MatchStage,
        candidate: ModelCandidate,
        candidate_index: int,
        confidence: float,
        reason: str,
    ) -> MatchResult:
        return MatchResult(
            stage=stage,
            candidate=candidate,
            candidate_index=candidate_index,
            confidence=max(0.0, min(1.0, confidence)),
            reason=reason,
            review_required=False,
        )

    @staticmethod
    def _review(
        reason: ReviewReason,
        detail: str,
        *,
        confidence: float = 0.0,
    ) -> MatchResult:
        return MatchResult(
            stage=MatchStage.REVIEW,
            confidence=max(0.0, min(1.0, confidence)),
            reason=detail,
            review_required=True,
            review_reason=reason,
        )

    def _ask_qwen(
        self, query: str, query_qualifiers: NameQualifiers
    ) -> MatchResult:
        if self.qwen_client is None:
            return self._review(
                ReviewReason.AMBIGUOUS,
                "deterministic matching was inconclusive and Qwen is disabled",
            )
        try:
            result = self.qwen_client.select_candidate(query, self.candidates)
        except Exception as exc:  # a plugin client must also fail closed
            return self._review(
                ReviewReason.CLIENT_ERROR,
                f"Qwen client failed safely: {type(exc).__name__}",
            )
        if not isinstance(result, QwenResult):
            return self._review(
                ReviewReason.INVALID_RESPONSE,
                "Qwen client returned an unvalidated result type",
            )
        if result.review_required or result.selection is None:
            return self._review(
                result.review_reason or ReviewReason.INVALID_RESPONSE,
                result.reason,
                confidence=(
                    result.selection.confidence if result.selection else 0.0
                ),
            )

        selection = result.selection
        index = selection.candidate_index
        if index is None or not 0 <= index < len(self.candidates):
            return self._review(
                ReviewReason.INVALID_CANDIDATE,
                "Qwen selected an index outside the candidate set",
            )

        compatible, _, conflict = _qualifier_check(
            query_qualifiers, self._candidate_qualifiers[index]
        )
        if not compatible:
            return self._review(
                ReviewReason.QUALIFIER_CONFLICT,
                f"Qwen selection rejected: {conflict}",
                confidence=selection.confidence,
            )
        return self._approved(
            MatchStage.QWEN,
            self.candidates[index],
            index,
            selection.confidence,
            selection.reason,
        )

    def match(self, query: str) -> MatchResult:
        if not isinstance(query, str) or not query.strip():
            return self._review(ReviewReason.UNKNOWN, "model name is blank")
        if not self.candidates:
            return self._review(ReviewReason.UNKNOWN, "candidate catalog is empty")

        normalized_query = normalize_model_name(query)
        query_qualifiers = extract_qualifiers(query)
        exact_indices: list[int] = []
        exact_conflicts: list[str] = []
        for index, candidate in enumerate(self.candidates):
            names = (candidate.canonical_name, *candidate.aliases)
            if normalized_query not in {
                normalize_model_name(name) for name in names
            }:
                continue
            compatible, _, conflict = _qualifier_check(
                query_qualifiers, self._candidate_qualifiers[index]
            )
            if compatible:
                exact_indices.append(index)
            elif conflict:
                exact_conflicts.append(conflict)

        if len(exact_indices) == 1:
            index = exact_indices[0]
            return self._approved(
                MatchStage.EXACT,
                self.candidates[index],
                index,
                1.0,
                "unique normalized canonical-name or alias match",
            )
        if len(exact_indices) > 1:
            return self._ask_qwen(query, query_qualifiers)

        scored: list[tuple[float, int]] = []
        conflicts: list[str] = exact_conflicts
        for index, candidate in enumerate(self.candidates):
            compatible, penalty, conflict = _qualifier_check(
                query_qualifiers, self._candidate_qualifiers[index]
            )
            if not compatible:
                if conflict:
                    conflicts.append(conflict)
                continue
            names = (candidate.canonical_name, *candidate.aliases)
            raw_score = max(
                _similarity(normalized_query, normalize_model_name(name))
                for name in names
            )
            scored.append((max(0.0, raw_score - penalty), index))

        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored:
            best_score, best_index = scored[0]
            next_score = scored[1][0] if len(scored) > 1 else -1.0
            if (
                best_score >= self.fuzzy_threshold
                and best_score - next_score >= self.ambiguity_margin
            ):
                return self._approved(
                    MatchStage.FUZZY,
                    self.candidates[best_index],
                    best_index,
                    best_score / 100.0,
                    (
                        f"qualifier-aware fuzzy score {best_score:.1f}; "
                        f"runner-up {next_score:.1f}"
                    ),
                )

        if self.qwen_client is not None:
            return self._ask_qwen(query, query_qualifiers)

        if not scored and conflicts:
            return self._review(
                ReviewReason.QUALIFIER_CONFLICT,
                f"all candidates conflict with critical qualifiers: {conflicts[0]}",
            )
        best_score = scored[0][0] if scored else 0.0
        reason = (
            ReviewReason.LOW_CONFIDENCE
            if scored and best_score < self.fuzzy_threshold
            else ReviewReason.AMBIGUOUS
        )
        return self._review(
            reason,
            (
                f"best qualifier-aware fuzzy score {best_score:.1f} did not meet "
                "the approval threshold and margin"
            ),
            confidence=best_score / 100.0,
        )


def match_model(
    query: str,
    candidates: Sequence[ModelCandidate | str | Mapping[str, Any]],
    **kwargs: Any,
) -> MatchResult:
    """Convenience wrapper for one-off matches."""

    return ModelMatcher(candidates, **kwargs).match(query)
