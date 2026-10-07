"""Unicode normalisation and safety-critical model-name qualifiers."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:[._-]\d+)*)(?!\d)")
_DATE_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:[-._/]?(?:0[1-9]|1[0-2])"
    r"(?:[-._/]?(?:0[1-9]|[12]\d|3[01]))?)?(?!\d)"
)
_RESOLUTION_RE = re.compile(r"(?<!\w)(?:\d{3,4}p|[248]k)(?!\w)", re.IGNORECASE)
_SIZE_RE = re.compile(r"(?<!\w)\d+(?:\.\d+)?\s*[bm](?!\w)", re.IGNORECASE)

_SIGNAL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "official": (
        re.compile(r"\bofficial\b", re.IGNORECASE),
        re.compile("\u5b98\u65b9"),
    ),
    "self_hosted": (
        re.compile(r"\bself[-\s]?host(?:ed|ing)?\b", re.IGNORECASE),
        re.compile(r"\bon[-\s]?prem(?:ise|ises)?\b", re.IGNORECASE),
        re.compile("\u81ea\u90e8\u7f72|\u672c\u5730\u90e8\u7f72"),
    ),
    "cache": (
        re.compile(r"\bcach(?:e|ed|ing)\b", re.IGNORECASE),
        re.compile("\u7f13\u5b58"),
    ),
    "fast": (
        re.compile(r"\bfast\b", re.IGNORECASE),
        re.compile("\u5feb\u901f"),
    ),
    "reference": (
        re.compile(r"\breference\b|\bref\b", re.IGNORECASE),
        re.compile("\u53c2\u8003"),
    ),
}


@dataclass(frozen=True, slots=True)
class NameQualifiers:
    versions: frozenset[str]
    dates: frozenset[str]
    resolutions: frozenset[str]
    sizes: frozenset[str]
    flags: frozenset[str]


def normalize_model_name(value: str) -> str:
    """NFKC/casefold text and collapse whitespace and punctuation.

    Punctuation is converted to separators rather than deleted, so unrelated
    tokens cannot accidentally be glued together.
    """

    if not isinstance(value, str):
        raise TypeError("model name must be a string")
    value = unicodedata.normalize("NFKC", value).casefold()
    chars: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if char.isalnum():
            chars.append(char)
        elif char.isspace() or category[0] in {"P", "S", "Z"}:
            chars.append(" ")
        else:
            chars.append(char)
    return _WHITESPACE_RE.sub(" ", "".join(chars)).strip()


def _normalise_numeric(value: str) -> str:
    return value.casefold().replace("_", ".").replace("-", ".").lstrip("v")


def extract_qualifiers(value: str) -> NameQualifiers:
    """Extract version/date/deployment/cache/resolution signals before fuzzing."""

    if not isinstance(value, str):
        raise TypeError("model name must be a string")
    folded = unicodedata.normalize("NFKC", value).casefold()

    dates = frozenset(
        re.sub(r"[-._/]", "", match.group(0)) for match in _DATE_RE.finditer(folded)
    )
    resolutions = frozenset(
        re.sub(r"\s+", "", match.group(0)).casefold()
        for match in _RESOLUTION_RE.finditer(folded)
    )
    sizes = frozenset(
        re.sub(r"\s+", "", match.group(0)).casefold()
        for match in _SIZE_RE.finditer(folded)
    )

    versions: set[str] = set()
    for match in _NUMBER_RE.finditer(folded):
        raw = match.group(1)
        compact = re.sub(r"[-._]", "", raw)
        if compact in dates:
            continue
        if match.group(0).casefold() in resolutions:
            continue
        versions.add(_normalise_numeric(raw))

    flags = frozenset(
        name
        for name, patterns in _SIGNAL_PATTERNS.items()
        if any(pattern.search(folded) for pattern in patterns)
    )
    return NameQualifiers(
        versions=frozenset(versions),
        dates=dates,
        resolutions=resolutions,
        sizes=sizes,
        flags=flags,
    )
