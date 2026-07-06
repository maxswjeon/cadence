"""D1 raw-boundary enforcement.

The canonical structured store (D1) and the derived-blob store (R2) may hold ONLY:

* structured/derived fields (enums, entity ids, normalized scalars),
* source-event IDs,
* hashes (hex digests),
* confidence values + confidence types,
* timestamps,
* short **non-verbatim** summaries.

They may **never** hold verbatim raw content: message/email/chat/audio text,
screenshots, raw OCR, account numbers, or verbatim transcript spans. Any verbatim
evidence lives in NAS (see :mod:`cadence.stores.nas`), referenced by opaque id/hash.

Two layers enforce this:

1. **Structural allowlist (the real guarantee)** — :class:`SchemaBoundary` knows the
   exact set of D1 columns and, per column, what *shape* of value is permitted. A write
   to D1 may reference **only** known columns, and free-text is accepted **only** on
   columns explicitly typed as bounded summary / enum / id / hash / label. Any unknown
   column, or free-text on a scalar/timestamp column, is a violation. This does not
   depend on guessing from a value or a field name.
2. **Heuristic classifier (best-effort defense-in-depth)** — :class:`PayloadClassifier`
   scans values for account/card numbers (separator-tolerant + Luhn), base64 blobs, and
   binary, plus a name-denylist. It backstops the structural layer and serves callers
   that have no table context; it is *not* the guarantee on its own.

A raw-to-D1 (or raw-to-R2) write is a **raw-to-cloud violation**.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, StrEnum

# --------------------------------------------------------------------------- #
# Verdicts & exceptions
# --------------------------------------------------------------------------- #


class Verdict(StrEnum):
    """Outcome of classifying a single field."""

    ALLOWED = "allowed"
    REJECTED = "rejected"


class RawBoundaryViolation(Exception):
    """Raised when a payload destined for D1/R2 contains verbatim raw content.

    Carries the offending ``field`` and a human-readable ``reason`` so the
    ``raw_to_cloud_violation`` alarm can log a non-verbatim summary of the breach.
    """

    def __init__(self, field: str, reason: str, tier: str = "D1") -> None:
        self.field = field
        self.reason = reason
        self.tier = tier
        super().__init__(f"raw-to-{tier} violation on field '{field}': {reason}")


@dataclass(frozen=True)
class FieldVerdict:
    """Per-field classification result."""

    field: str
    verdict: Verdict
    reason: str = ""


# --------------------------------------------------------------------------- #
# Denylists / patterns
# --------------------------------------------------------------------------- #

# Whole-token names that imply verbatim raw content (checked after tokenizing).
_RAW_FIELD_TOKENS: frozenset[str] = frozenset(
    {
        "raw", "body", "text", "content", "message", "email", "chat", "transcript",
        "audio", "voice", "screenshot", "screen", "ocr", "snippet", "verbatim",
        "attachment", "photo", "image", "account_number", "acct_number", "card_number",
        "iban", "ssn", "password", "secret", "token", "credential",
    }
)

# High-risk tokens matched as raw SUBSTRINGS (not just whole tokens), to catch
# concatenated bypasses like ``emailbody`` / ``messagebody`` / ``chattranscript``.
# Deliberately excludes ``email``/``message``/``account`` (would false-positive on
# legitimate opaque columns like ``account_ref``); those stay whole-token only.
_RAW_SUBSTRING_TOKENS: tuple[str, ...] = (
    "body", "transcript", "chat", "ocr", "screenshot", "audio", "verbatim",
)

# Field names that *contain* a raw token but are explicitly structured/safe.
_ALLOWED_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "content_hash", "text_hash", "raw_hash", "raw_evidence_hash", "raw_evidence_id",
        "raw_pointer", "context_type",
    }
)

# Suffixes that denote a safe structured value even if the stem is a raw token.
_SAFE_SUFFIXES: tuple[str, ...] = ("_id", "_ids", "_hash", "_pointer", "_ref", "_uri", "_count")

# Field names that are explicitly non-verbatim summaries (length-bounded, still scanned).
_SUMMARY_SUFFIXES: tuple[str, ...] = ("summary", "_summary")

# A hex digest (sha256 = 64 hex chars; also accept 32/40/128).
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{32,128}$", re.IGNORECASE)

# ISO-8601-ish timestamps/dates are structured, never raw — never flag them.
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+\-]\d{2}:?\d{2})?)?$"
)

# Account/card-number detection: separators may be space, dot, or dash.
_SEPARATORS_RE = re.compile(r"[ .\-]")
_DIGIT_RUN_12_RE = re.compile(r"\d{12,}")
# A card-length grouped-digit candidate (13-19 digits with optional separators).
_CARD_CANDIDATE_RE = re.compile(r"\d(?:[ .\-]?\d){12,18}")

# Base64 blob: one long whitespace-free token of base64 charset.
_B64_CHARSET_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_B64_MIN_LEN = 64

# Scalar types that are always structured-safe (never stringified for raw scanning).
_SAFE_SCALAR_TYPES = (bool, int, float, datetime, date, time, Decimal, uuid.UUID)


def _strip_separators(text: str) -> str:
    return _SEPARATORS_RE.sub("", text)


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — a secondary signal that a digit run is a real card number."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _looks_like_account_number(text: str) -> bool:
    """True if, after stripping ``[ .-]`` separators, a >=12-digit run remains."""
    return bool(_DIGIT_RUN_12_RE.search(_strip_separators(text)))


def _has_luhn_card(text: str) -> bool:
    """True if any 13-19 digit grouped candidate passes the Luhn checksum."""
    for match in _CARD_CANDIDATE_RE.finditer(text):
        digits = _strip_separators(match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def _looks_like_base64_blob(text: str) -> bool:
    """True if a long, whitespace-free, base64-charset token suggests an encoded blob."""
    for token in text.split():
        if len(token) < _B64_MIN_LEN or not _B64_CHARSET_RE.match(token):
            continue
        if _HEX_DIGEST_RE.match(token):
            continue  # a long hex hash is a hash, allowed elsewhere
        core = token.rstrip("=")
        signals = (
            "+" in token
            or "/" in token
            or token.endswith("=")
            or (any(c.isupper() for c in core) and any(c.isdigit() for c in core))
        )
        if signals:
            return True
    return False


def _content_violation(text: str) -> str | None:
    """Return a rejection reason if free text contains raw content, else ``None``."""
    scanned = text.replace("\n", " ")
    if _looks_like_account_number(scanned):
        return "value contains an account/card-number-like digit run (>=12 digits)"
    if _has_luhn_card(scanned):
        return "value contains a Luhn-valid card-number sequence"
    if _looks_like_base64_blob(text):
        return "value looks like a base64-encoded blob (likely verbatim raw)"
    return None


# --------------------------------------------------------------------------- #
# Heuristic classifier (defense-in-depth)
# --------------------------------------------------------------------------- #


@dataclass
class PayloadClassifier:
    """Best-effort content/name classifier for values bound for D1/R2.

    This backstops the structural :class:`SchemaBoundary`; it is not the guarantee on
    its own (a non-denylisted field name can still accept a bounded label). Use
    :meth:`SchemaBoundary.enforce_row` on the write path for the real allowlist.
    """

    max_summary_len: int = 500
    max_structured_text_len: int = 280
    extra_allowed_fields: frozenset[str] = field(default_factory=frozenset)

    # -- field-name analysis ------------------------------------------------ #

    def _field_is_raw_named(self, name: str) -> bool:
        lname = name.lower()
        if lname in _ALLOWED_FIELD_NAMES or lname in self.extra_allowed_fields:
            return False
        if lname.endswith(_SAFE_SUFFIXES) or lname.endswith(_SUMMARY_SUFFIXES):
            return False
        tokens = set(re.split(r"[^a-z0-9]+", lname))
        if tokens & _RAW_FIELD_TOKENS:
            return True
        # substring match for high-risk concatenated names (emailbody, messagebody, ...)
        return any(sub in lname for sub in _RAW_SUBSTRING_TOKENS)

    @staticmethod
    def _is_summary_field(name: str) -> bool:
        return name.lower().endswith(_SUMMARY_SUFFIXES)

    # -- value analysis ----------------------------------------------------- #

    def _classify_value(self, name: str, value: object) -> FieldVerdict:
        if value is None or isinstance(value, _SAFE_SCALAR_TYPES) or isinstance(value, Enum):
            return FieldVerdict(name, Verdict.ALLOWED)

        if isinstance(value, (bytes, bytearray, memoryview)):
            return FieldVerdict(
                name, Verdict.REJECTED,
                "binary blob (bytes) may not enter D1/R2; store raw in NAS by hash",
            )

        if isinstance(value, (list, tuple, set)):
            for item in value:
                v = self._classify_value(name, item)
                if v.verdict is Verdict.REJECTED:
                    return v
            return FieldVerdict(name, Verdict.ALLOWED)

        if isinstance(value, dict):
            for k, item in value.items():
                v = self.classify_field(str(k), item)
                if v.verdict is Verdict.REJECTED:
                    return v
            return FieldVerdict(name, Verdict.ALLOWED)

        text = str(value)
        stripped = text.strip()
        if _HEX_DIGEST_RE.match(stripped) or _ISO_DATETIME_RE.match(stripped):
            return FieldVerdict(name, Verdict.ALLOWED)

        reason = _content_violation(text)
        if reason is not None:
            return FieldVerdict(name, Verdict.REJECTED, reason)

        budget = (
            self.max_summary_len
            if self._is_summary_field(name)
            else self.max_structured_text_len
        )
        if len(text) > budget:
            return FieldVerdict(
                name, Verdict.REJECTED,
                f"free-text length {len(text)} exceeds {budget} (may carry verbatim raw)",
            )
        return FieldVerdict(name, Verdict.ALLOWED)

    # -- public API --------------------------------------------------------- #

    def classify_field(self, name: str, value: object) -> FieldVerdict:
        """Classify a single field/value pair (name analysis first, then content)."""
        if self._field_is_raw_named(name):
            return FieldVerdict(
                name, Verdict.REJECTED,
                "field name denotes verbatim raw content (message/email/audio/screen/"
                "account-number/credential); keep raw in NAS and reference by id/hash",
            )
        return self._classify_value(name, value)

    def classify(self, payload: dict[str, object]) -> list[FieldVerdict]:
        """Classify every field in a payload; returns all verdicts (allowed + rejected)."""
        return [self.classify_field(name, value) for name, value in payload.items()]

    def enforce(self, payload: dict[str, object], *, tier: str = "D1") -> None:
        """Raise :class:`RawBoundaryViolation` on the first rejected field (heuristic)."""
        for verdict in self.classify(payload):
            if verdict.verdict is Verdict.REJECTED:
                raise RawBoundaryViolation(verdict.field, verdict.reason, tier=tier)

    # -- helper used by the structural layer -------------------------------- #

    def scan_string_reason(self, text: str, budget: int) -> str | None:
        """Return a rejection reason for a free-text string against a length budget."""
        stripped = text.strip()
        if _HEX_DIGEST_RE.match(stripped) or _ISO_DATETIME_RE.match(stripped):
            return None
        reason = _content_violation(text)
        if reason is not None:
            return reason
        if len(text) > budget:
            return f"length {len(text)} exceeds budget {budget} (may carry verbatim raw)"
        return None


# --------------------------------------------------------------------------- #
# Structural allowlist (the real guarantee)
# --------------------------------------------------------------------------- #


class ColumnKind(StrEnum):
    """The shape of value a D1 column may carry."""

    SCALAR = "scalar"    # numbers/bool/datetime — never free-text
    JSON = "json"        # structured JSON (recursively scanned)
    ID = "id"            # opaque id / fk / stable key
    HASH = "hash"        # hex digest / provenance pointer
    ENUM = "enum"        # short controlled vocabulary
    LABEL = "label"      # short non-verbatim label/title
    SUMMARY = "summary"  # bounded non-verbatim summary


# String columns whose names denote a short controlled vocabulary.
_ENUM_NAMES: frozenset[str] = frozenset(
    {"status", "origin", "acquisition_tier", "confidence_type", "signal", "kind", "provider"}
)

_ENUM_BUDGET = 64
_ID_BUDGET = 128
_DEFAULT_LABEL_BUDGET = 255


@dataclass(frozen=True)
class ColumnPolicy:
    """The permitted shape + length budget for one column."""

    name: str
    kind: ColumnKind
    budget: int | None = None


def derive_column_policy(name: str, sql_kind: str, max_len: int | None) -> ColumnPolicy:
    """Map a column (name + coarse SQL kind + declared length) to a :class:`ColumnPolicy`.

    ``sql_kind`` is one of ``"scalar"``, ``"json"``, ``"text"``, ``"string"``.
    """
    lname = name.lower()
    if sql_kind == "scalar":
        return ColumnPolicy(name, ColumnKind.SCALAR)
    if sql_kind == "json":
        return ColumnPolicy(name, ColumnKind.JSON)
    if sql_kind == "text":
        return ColumnPolicy(name, ColumnKind.SUMMARY)  # budget resolved against classifier
    # string columns
    if lname.endswith("hash"):
        return ColumnPolicy(name, ColumnKind.HASH, max_len or _ID_BUDGET)
    if lname == "id" or lname.endswith(("_id", "_key")):
        return ColumnPolicy(name, ColumnKind.ID, max_len or _ID_BUDGET)
    if lname.endswith(_SUMMARY_SUFFIXES):
        return ColumnPolicy(name, ColumnKind.SUMMARY)
    if lname in _ENUM_NAMES:
        return ColumnPolicy(name, ColumnKind.ENUM, _ENUM_BUDGET)
    return ColumnPolicy(name, ColumnKind.LABEL, max_len or _DEFAULT_LABEL_BUDGET)


@dataclass
class TableBoundary:
    """The column allowlist for one table."""

    table: str
    columns: dict[str, ColumnPolicy]


@dataclass
class SchemaBoundary:
    """Per-table column allowlist enforcing the D1 raw boundary structurally."""

    tables: dict[str, TableBoundary]
    classifier: PayloadClassifier

    def _budget(self, policy: ColumnPolicy) -> int:
        if policy.kind is ColumnKind.SUMMARY:
            return self.classifier.max_summary_len
        return policy.budget or _DEFAULT_LABEL_BUDGET

    def _check_column(self, policy: ColumnPolicy, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "binary blob may not enter D1/R2; store raw in NAS by hash"
        if policy.kind is ColumnKind.SCALAR:
            if isinstance(value, str):
                return "scalar/timestamp column may not carry free-text"
            return None
        if policy.kind is ColumnKind.JSON:
            verdict = self.classifier._classify_value(policy.name, value)  # noqa: SLF001
            return None if verdict.verdict is Verdict.ALLOWED else verdict.reason
        # string-shaped kinds (ID/HASH/ENUM/LABEL/SUMMARY)
        if not isinstance(value, str):
            return None  # numbers coerced into a string column are structured-safe
        return self.classifier.scan_string_reason(value, self._budget(policy))

    def enforce_row(self, table: str, values: dict[str, object], *, tier: str = "D1") -> None:
        """Enforce the allowlist for one row; raise on the first violation.

        * Unknown table or column → violation (nothing outside the schema may be written).
        * Each value must match its column's permitted shape + length budget.
        """
        tb = self.tables.get(table)
        if tb is None:
            raise RawBoundaryViolation(
                "<table>", f"table '{table}' is not in the D1 allowlist", tier
            )
        for name, value in values.items():
            policy = tb.columns.get(name)
            if policy is None:
                raise RawBoundaryViolation(
                    name, f"column '{name}' is not in the allowlist for table '{table}'", tier
                )
            reason = self._check_column(policy, value)
            if reason is not None:
                raise RawBoundaryViolation(name, reason, tier)


def build_schema_boundary(
    table_specs: dict[str, list[tuple[str, str, int | None]]],
    classifier: PayloadClassifier | None = None,
) -> SchemaBoundary:
    """Build a :class:`SchemaBoundary` from ``{table: [(col, sql_kind, max_len), ...]}``."""
    classifier = classifier or PayloadClassifier()
    tables: dict[str, TableBoundary] = {}
    for table, cols in table_specs.items():
        policies = {c[0]: derive_column_policy(c[0], c[1], c[2]) for c in cols}
        tables[table] = TableBoundary(table=table, columns=policies)
    return SchemaBoundary(tables=tables, classifier=classifier)


# --------------------------------------------------------------------------- #
# Schema lint (model-level guard)
# --------------------------------------------------------------------------- #


def lint_columns(table_name: str, column_names: list[str]) -> list[str]:
    """Return column names that violate the raw boundary *by name* (defensive lint)."""
    classifier = PayloadClassifier()
    return [c for c in column_names if classifier._field_is_raw_named(c)]  # noqa: SLF001


__all__ = [
    "Verdict",
    "FieldVerdict",
    "RawBoundaryViolation",
    "PayloadClassifier",
    "ColumnKind",
    "ColumnPolicy",
    "TableBoundary",
    "SchemaBoundary",
    "derive_column_policy",
    "build_schema_boundary",
    "lint_columns",
]
