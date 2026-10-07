from __future__ import annotations

from pydantic import ValidationError
import pytest

from tokensql.llm import (
    MatchStage,
    ModelCandidate,
    QwenResult,
    QwenSelection,
    ReviewReason,
)
from tokensql.matching import ModelMatcher, extract_qualifiers, normalize_model_name


class _Selector:
    def __init__(self, result: QwenResult) -> None:
        self.result = result
        self.calls = 0

    def select_candidate(self, query, candidates):
        self.calls += 1
        return self.result


def _approved(index: int, confidence: float = 0.95) -> QwenResult:
    selection = QwenSelection(
        candidate_index=index,
        confidence=confidence,
        reason="semantic tie-break",
        abstain=False,
    )
    return QwenResult(
        selection=selection,
        review_required=False,
        reason=selection.reason,
    )


def test_nfkc_casefold_whitespace_and_punctuation_normalization() -> None:
    assert normalize_model_name("  ＱＷＥＮ—２．５／３２Ｂ  ") == "qwen 2 5 32b"


def test_exact_alias_has_priority() -> None:
    matcher = ModelMatcher(
        [
            ModelCandidate(
                canonical_name="Qwen2.5-32B-Instruct",
                aliases=("qwen 2.5 32b instruct",),
            ),
            ModelCandidate(canonical_name="Qwen2.5-7B-Instruct"),
        ]
    )
    result = matcher.match(" QWEN 2.5／32B instruct ")
    assert result.stage is MatchStage.EXACT
    assert result.candidate_index == 0
    assert not result.review_required


def test_unique_typo_can_fuzzy_match() -> None:
    matcher = ModelMatcher(
        [
            "Claude 3.5 Sonnet",
            "Gemini 1.5 Pro",
        ],
        fuzzy_threshold=80,
    )
    result = matcher.match("claude 3.5 sonet")
    assert result.stage is MatchStage.FUZZY
    assert result.candidate_index == 0


@pytest.mark.parametrize(
    ("query", "candidate"),
    [
        ("GPT-4.1", "GPT-4"),
        ("Claude Sonnet 2024-10-22", "Claude Sonnet 2024-06-20"),
        ("Vision Model 4k", "Vision Model 1080p"),
        ("Qwen 32B official", "Qwen 32B self-hosted"),
        ("Qwen 32B cache", "Qwen 32B"),
        ("Image fast reference", "Image reference"),
    ],
)
def test_critical_qualifier_conflicts_never_auto_approve(
    query: str, candidate: str
) -> None:
    result = ModelMatcher([candidate], fuzzy_threshold=0).match(query)
    assert result.stage is MatchStage.REVIEW
    assert result.review_required
    assert result.review_reason is ReviewReason.QUALIFIER_CONFLICT


def test_qualifier_extraction_preserves_required_signals() -> None:
    qualifiers = extract_qualifiers(
        "Model v2.1 2024-10-22 official self-hosted cache 4K fast reference 32B"
    )
    assert "2.1" in qualifiers.versions
    assert "20241022" in qualifiers.dates
    assert "4k" in qualifiers.resolutions
    assert "32b" in qualifiers.sizes
    assert qualifiers.flags == {
        "official",
        "self_hosted",
        "cache",
        "fast",
        "reference",
    }


def test_fuzzy_collision_goes_to_review_without_qwen() -> None:
    matcher = ModelMatcher(
        ["Alpha Model v2", "Alpha Model v2 Preview"],
        fuzzy_threshold=70,
        ambiguity_margin=20,
    )
    result = matcher.match("Alpha Model v2 Previw")
    assert result.stage is MatchStage.REVIEW
    assert result.review_required


def test_qwen_may_only_choose_from_candidates() -> None:
    selector = _Selector(_approved(1))
    matcher = ModelMatcher(
        [
            ModelCandidate(canonical_name="Model A", aliases=("shared",)),
            ModelCandidate(canonical_name="Model B", aliases=("shared",)),
        ],
        qwen_client=selector,
    )
    result = matcher.match("shared")
    assert selector.calls == 1
    assert result.stage is MatchStage.QWEN
    assert result.candidate_index == 1


def test_matcher_rechecks_qwen_selection_qualifiers() -> None:
    selector = _Selector(_approved(0))
    matcher = ModelMatcher(
        ["GPT-4", "GPT-4.1"],
        qwen_client=selector,
        fuzzy_threshold=100,
    )
    result = matcher.match("GPT-4.1 official")
    assert result.review_required
    assert result.review_reason is ReviewReason.QUALIFIER_CONFLICT
    assert result.candidate is None


def test_qwen_low_confidence_review_is_propagated() -> None:
    selection = QwenSelection(
        candidate_index=0,
        confidence=0.3,
        reason="uncertain",
        abstain=False,
    )
    selector = _Selector(
        QwenResult(
            selection=selection,
            review_required=True,
            review_reason=ReviewReason.LOW_CONFIDENCE,
            reason="below threshold",
        )
    )
    result = ModelMatcher(["A", "B"], qwen_client=selector).match("unknown")
    assert result.review_required
    assert result.review_reason is ReviewReason.LOW_CONFIDENCE
    assert result.candidate is None


def test_candidate_schema_rejects_database_and_sql_fields() -> None:
    with pytest.raises(ValidationError):
        ModelCandidate.model_validate(
            {
                "canonical_name": "Qwen",
                "database_id": 123,
                "primary_key": "pk",
                "sql": "SELECT * FROM models",
            }
        )
