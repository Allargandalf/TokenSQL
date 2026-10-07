"""Types and orchestration for official-price sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .http import OfficialHttpClient


@dataclass(frozen=True)
class OfficialRecord:
    """A source-derived record that still requires domain verification."""

    values: Mapping[str, Any]
    source_url: str
    observed_at: datetime
    raw_sha256: str


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    source_url: str
    fetched_at: datetime
    status: str
    raw_sha256: str | None = None
    raw_body: bytes | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    records: tuple[OfficialRecord, ...] = ()
    review_reasons: tuple[str, ...] = ()
    attempts: int = 0

    @property
    def needs_review(self) -> bool:
        return self.status != "fetched"


class OfficialSource(ABC):
    """Independent source adapter; it never writes to the database."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def parse(self, result: FetchResult) -> tuple[Mapping[str, Any], ...]:
        """Extract structured candidates without approving their prices."""

    def collect(self, client: OfficialHttpClient, url: str) -> FetchResult:
        result = client.fetch(url)
        if result.needs_review:
            return result
        try:
            parsed = self.parse(result)
            records = tuple(
                OfficialRecord(
                    values=dict(item),
                    source_url=result.source_url,
                    observed_at=result.fetched_at,
                    raw_sha256=result.raw_sha256 or "",
                )
                for item in parsed
            )
        except Exception as exc:
            return replace(
                result,
                status="pending_review",
                records=(),
                review_reasons=(f"source parser failed: {type(exc).__name__}: {exc}",),
            )
        return replace(result, records=records)
