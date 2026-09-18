"""Parse and validate a tablewarden TOML file into typed check definitions.

Validation is done by hand rather than with a schema library so that every
error names the exact table path that is wrong (`check[3].columns`) and
says what was expected. A data-quality tool whose own config errors are
vague would be a poor advertisement.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DURATION = re.compile(r"^(\d+)\s*(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

Severity = Literal["error", "warn"]
SourceKind = Literal["postgres", "sqlite", "csv"]


class ConfigError(ValueError):
    """Raised with a message that starts with the config path at fault."""


@dataclass(frozen=True, slots=True)
class Source:
    kind: SourceKind
    #: Environment variable holding the DSN (postgres) -- never the DSN itself.
    dsn_env: str | None = None
    #: File path for sqlite; directory of CSV files for csv.
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class Check:
    """One check. Fields not relevant to `kind` are left at their defaults."""

    kind: str
    name: str
    severity: Severity = "error"
    table: str | None = None
    column: str | None = None
    columns: tuple[str, ...] = ()
    where: str | None = None
    values: tuple[str | int | float | bool, ...] = ()
    max_rate: float | None = None
    min_rows: int | None = None
    max_rows: int | None = None
    to_table: str | None = None
    to_column: str | None = None
    max_age_seconds: int | None = None
    query: str | None = None
    expect: int | None = None
    expect_max: int | None = None
    sample: int = 5
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Config:
    source: Source
    checks: tuple[Check, ...]


KINDS = {
    "not_null",
    "unique",
    "accepted_values",
    "null_rate",
    "row_count",
    "references",
    "freshness",
    "sql",
}


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not valid TOML: {error}") from error
    return parse(raw, base=path.parent)


def parse(raw: dict[str, Any], base: Path = Path()) -> Config:
    source = _parse_source(_expect_table(raw, "source"), base)
    raw_checks = raw.get("check")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ConfigError("check: expected at least one [[check]] table")
    checks = tuple(_parse_check(entry, f"check[{i}]") for i, entry in enumerate(raw_checks))
    names = [c.name for c in checks]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"check: duplicate names {duplicates}; give each check a unique `name`")
    return Config(source=source, checks=checks)


def _expect_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: expected a table")
    return value


def _parse_source(raw: dict[str, Any], base: Path) -> Source:
    kind = raw.get("kind")
    if kind not in ("postgres", "sqlite", "csv"):
        raise ConfigError("source.kind: expected one of postgres, sqlite, csv")
    if kind == "postgres":
        dsn_env = raw.get("dsn_env")
        if not isinstance(dsn_env, str) or not dsn_env:
            raise ConfigError("source.dsn_env: expected the name of an environment variable holding the DSN")
        if "dsn" in raw:
            raise ConfigError("source.dsn: connection strings do not belong in config files; use dsn_env")
        return Source(kind="postgres", dsn_env=dsn_env)
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise ConfigError(f"source.path: expected a {'file' if kind == 'sqlite' else 'directory'} path")
    return Source(kind=kind, path=(base / path))


def _parse_check(raw: Any, at: str) -> Check:  # noqa: ANN401, PLR0912, PLR0915
    if not isinstance(raw, dict):
        raise ConfigError(f"{at}: expected a table")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ConfigError(f"{at}.kind: expected one of {sorted(KINDS)}")

    severity = raw.get("severity", "error")
    if severity not in ("error", "warn"):
        raise ConfigError(f"{at}.severity: expected error or warn")

    table = _opt_identifier(raw, "table", at)
    column = _opt_identifier(raw, "column", at)
    columns = _opt_identifiers(raw, "columns", at)
    where = raw.get("where")
    if where is not None and not isinstance(where, str):
        raise ConfigError(f"{at}.where: expected a SQL boolean expression string")
    sample = raw.get("sample", 5)
    if not isinstance(sample, int) or isinstance(sample, bool) or sample < 0 or sample > 100:
        raise ConfigError(f"{at}.sample: expected an integer 0..100")

    fields: dict[str, Any] = {}

    if kind == "sql":
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ConfigError(f"{at}.query: expected a SQL query returning a single number")
        expect, expect_max = raw.get("expect"), raw.get("expect_max")
        if (expect is None) == (expect_max is None):
            raise ConfigError(f"{at}: give exactly one of expect or expect_max")
        for key, value in (("expect", expect), ("expect_max", expect_max)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ConfigError(f"{at}.{key}: expected an integer")
        fields.update(query=query, expect=expect, expect_max=expect_max)
    else:
        if table is None:
            raise ConfigError(f"{at}.table: required for {kind}")
        if kind in ("not_null", "accepted_values", "null_rate", "references", "freshness") and column is None:
            raise ConfigError(f"{at}.column: required for {kind}")
        if kind == "unique":
            if not columns and column is None:
                raise ConfigError(f"{at}.columns: required for unique (one or more columns)")
            if column is not None and not columns:
                columns = (column,)
        if kind == "accepted_values":
            values = raw.get("values")
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(v, str | int | float | bool) for v in values)
            ):
                raise ConfigError(f"{at}.values: expected a non-empty list of strings, numbers or booleans")
            fields["values"] = tuple(values)
        if kind == "null_rate":
            rate = raw.get("max_rate")
            if not isinstance(rate, int | float) or isinstance(rate, bool) or not 0 <= rate <= 1:
                raise ConfigError(f"{at}.max_rate: expected a number between 0 and 1")
            fields["max_rate"] = float(rate)
        if kind == "row_count":
            lo, hi = raw.get("min"), raw.get("max")
            if lo is None and hi is None:
                raise ConfigError(f"{at}: row_count needs min, max or both")
            for key, value in (("min", lo), ("max", hi)):
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise ConfigError(f"{at}.{key}: expected a non-negative integer")
            fields.update(min_rows=lo, max_rows=hi)
        if kind == "references":
            to_table = _opt_identifier(raw, "to_table", at)
            to_column = _opt_identifier(raw, "to_column", at)
            if to_table is None or to_column is None:
                raise ConfigError(f"{at}: references needs to_table and to_column")
            fields.update(to_table=to_table, to_column=to_column)
        if kind == "freshness":
            max_age = raw.get("max_age")
            match = DURATION.match(max_age) if isinstance(max_age, str) else None
            if match is None:
                raise ConfigError(f"{at}.max_age: expected a duration like 30m, 2h or 1d")
            fields["max_age_seconds"] = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]

    default_name = _default_name(kind, table, column, columns)
    name = raw.get("name", default_name)
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{at}.name: expected a non-empty string")

    known = {
        "kind", "name", "severity", "table", "column", "columns", "where", "values", "max_rate",
        "min", "max", "to_table", "to_column", "max_age", "query", "expect", "expect_max", "sample",
    }  # fmt: skip
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"{at}: unknown keys {unknown}")

    return Check(
        kind=kind, name=name, severity=severity, table=table, column=column, columns=columns,
        where=where, sample=sample, **fields,
    )  # fmt: skip


def _opt_identifier(raw: dict[str, Any], key: str, at: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        raise ConfigError(f"{at}.{key}: expected an unquoted SQL identifier (letters, digits, underscore)")
    return value


def _opt_identifiers(raw: dict[str, Any], key: str, at: str) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and IDENTIFIER.match(v) for v in value):
        raise ConfigError(f"{at}.{key}: expected a non-empty list of SQL identifiers")
    return tuple(value)


def _default_name(kind: str, table: str | None, column: str | None, columns: tuple[str, ...]) -> str:
    target = column or (".".join(columns) if columns else None)
    parts = [p for p in (table, kind, target) if p]
    return ".".join(parts) if parts else kind
