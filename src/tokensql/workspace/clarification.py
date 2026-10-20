"""Strict model-to-host clarification output, separate from a database proposal."""
from __future__ import annotations

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


QuestionText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True,
                                               min_length=1, max_length=2000)]
SummaryText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=4000)]


class NeedsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[QuestionText] = Field(min_length=1, max_length=20)
    summary: SummaryText | None = None
