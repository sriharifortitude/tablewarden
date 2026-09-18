"""Compile a Check into SQL and interpret the rows that come back.

Identifiers are double-quoted after the config layer has restricted them
to [A-Za-z0-9_]; values travel as parameters. The one place user text is
spliced into SQL is `where`, which is a SQL expression by definition --
the config is code, and the README says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .config import Check

Status = Literal["pass", "fail", "error"]


@dataclass(frozen=True, slots=True)
class Statement:
    sql: str
    params: tuple[Any, ...] = ()

    def for_placeholder(self, placeholder: str) -> str:
        """Compiled SQL uses `?`; psycopg wants `%s`. Only the placeholders change."""
        return self.sql if placeholder == "?" else self.sql.replace("?", placeholder)


@dataclass(frozen=True, slots=True)
class Compiled:
    #: Returns one row whose first column(s) decide the outcome.
    measure: Statement
    #: Optional: rows to show when the check fails.
    sample: Statement | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    #: One line a human can read: "3 rows", "rate 0.071 > 0.05", "max(created_at) is 3h 12m old".
    observed: str
    #: Number of offending rows/keys when that is meaningful.
    failures: int | None = None
    sample: tuple[tuple[Any, ...], ...] = ()
    sample_columns: tuple[str, ...] = ()
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def q(identifier: str) -> str:
    return f'"{identifier}"'


def _where(check: Check, *conditions: str) -> str:
    parts = [*conditions]
    if check.where:
        parts.append(f"({check.where})")
    return f" where {' and '.join(parts)}" if parts else ""


def compile_check(check: Check) -> Compiled:  # noqa: PLR0911
    table = q(check.table) if check.table else ""
    column = q(check.column) if check.column else ""
    limit = check.sample

    def top_values(where: str) -> str:
        return f"select {column}, count(*) as n from {table}{where} group by {column} order by n desc limit {limit}"

    match check.kind:
        case "not_null":
            where = _where(check, f"{column} is null")
            return Compiled(
                Statement(f"select count(*) from {table}{where}"),
                Statement(f"select * from {table}{where} limit {limit}") if limit else None,
            )
        case "unique":
            cols = ", ".join(q(c) for c in check.columns)
            where = _where(check)
            inner = f"select {cols}, count(*) as n from {table}{where} group by {cols} having count(*) > 1"
            return Compiled(
                Statement(f"select count(*) from ({inner}) as duplicated"),
                Statement(f"{inner} order by n desc limit {limit}") if limit else None,
            )
        case "accepted_values":
            marks = ", ".join("?" for _ in check.values)
            where = _where(check, f"{column} is not null", f"{column} not in ({marks})")
            return Compiled(
                Statement(f"select count(*) from {table}{where}", check.values),
                Statement(top_values(where), check.values) if limit else None,
            )
        case "null_rate":
            where = _where(check)
            return Compiled(
                Statement(f"select count(*), sum(case when {column} is null then 1 else 0 end) from {table}{where}")
            )
        case "row_count":
            return Compiled(Statement(f"select count(*) from {table}{_where(check)}"))
        case "references":
            assert check.to_table is not None and check.to_column is not None
            parent = f"select {q(check.to_column)} from {q(check.to_table)} where {q(check.to_column)} is not null"
            where = _where(check, f"{column} is not null", f"{column} not in ({parent})")
            return Compiled(
                Statement(f"select count(*) from {table}{where}"),
                Statement(top_values(where)) if limit else None,
            )
        case "freshness":
            return Compiled(Statement(f"select max({column}) from {table}{_where(check)}"))
        case "sql":
            assert check.query is not None
            return Compiled(Statement(check.query))
    raise AssertionError(f"unreachable: {check.kind}")


def interpret(check: Check, measured: Sequence[tuple[Any, ...]], now: datetime | None = None) -> Outcome:  # noqa: PLR0911
    """Turn the measure statement's rows into pass/fail with a readable observation."""
    row = measured[0] if measured else ()
    match check.kind:
        case "not_null" | "unique" | "accepted_values" | "references":
            count = _int(row[0]) if row else 0
            noun = {"unique": "duplicated key", "references": "orphan row"}.get(check.kind, "row")
            return Outcome(
                "pass" if count == 0 else "fail",
                f"{count} {noun}{'' if count == 1 else 's'}",
                failures=count,
            )
        case "null_rate":
            total = _int(row[0]) if row else 0
            nulls = _int(row[1]) if row and row[1] is not None else 0
            rate = 0.0 if total == 0 else nulls / total
            assert check.max_rate is not None
            return Outcome(
                "pass" if rate <= check.max_rate else "fail",
                f"rate {rate:.4f} ({nulls} of {total}) {'<=' if rate <= check.max_rate else '>'} {check.max_rate}",
                failures=nulls,
                extra={"rate": rate, "total": total, "nulls": nulls},
            )
        case "row_count":
            count = _int(row[0]) if row else 0
            lo, hi = check.min_rows, check.max_rows
            ok = (lo is None or count >= lo) and (hi is None or count <= hi)
            bounds = f"[{lo if lo is not None else ''}, {hi if hi is not None else ''}]"
            return Outcome("pass" if ok else "fail", f"{count} rows, expected within {bounds}", extra={"count": count})
        case "freshness":
            latest = _timestamp(row[0]) if row else None
            assert check.max_age_seconds is not None
            if latest is None:
                return Outcome("fail", f"no non-null {check.column} values", failures=1)
            now = now or datetime.now(UTC)
            age = (now - latest).total_seconds()
            return Outcome(
                "pass" if age <= check.max_age_seconds else "fail",
                f"max({check.column}) is {_age(age)} old, limit {_age(check.max_age_seconds)}",
                extra={"latest": latest.isoformat(), "age_seconds": age},
            )
        case "sql":
            if not row:
                return Outcome("error", "query returned no rows", message="a sql check must return one row")
            value = _int(row[0])
            if check.expect is not None:
                ok = value == check.expect
                return Outcome(
                    "pass" if ok else "fail",
                    f"{value} {'==' if ok else '!='} {check.expect}",
                    extra={"value": value},
                )
            assert check.expect_max is not None
            ok = value <= check.expect_max
            return Outcome(
                "pass" if ok else "fail",
                f"{value} {'<=' if ok else '>'} {check.expect_max}",
                extra={"value": value},
            )
    raise AssertionError(f"unreachable: {check.kind}")


def _int(value: Any) -> int:  # noqa: ANN401
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    raise TypeError(f"expected a number, got {type(value).__name__}: {value!r}")


def _timestamp(value: Any) -> datetime | None:  # noqa: ANN401
    """Postgres returns datetimes; SQLite returns text. Naive values are taken as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace(" ", "T", 1) if " " in value and "T" not in value else value)
    else:
        raise TypeError(f"freshness column must be a timestamp, got {type(value).__name__}: {value!r}")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s}s" if s else f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {m}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if h else f"{days}d"
