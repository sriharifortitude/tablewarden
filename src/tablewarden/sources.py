"""Where the rows live. Every source is driven through the same two calls.

Postgres, SQLite and CSV all end up as SQL: CSV files are loaded into an
in-memory SQLite database with light type inference, which is what lets a
CSV get every check the SQL sources get without a second implementation.

Postgres sessions are read-only at the transaction level. A check is a
question; nothing in this tool should be able to answer it by changing
the data, and `sql` checks are user-supplied text.
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from .config import IDENTIFIER, Source

Row = tuple[Any, ...]
#: Column names, then rows.
Result = tuple[list[str], list[Row]]


class Connection(Protocol):
    #: The parameter placeholder the driver expects: "?" or "%s".
    placeholder: str

    def query(self, sql: str, params: Sequence[Any] = ()) -> Result: ...


class SqliteConnection:
    placeholder = "?"

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def query(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cursor = self._connection.execute(sql, tuple(params))
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return columns, [tuple(row) for row in cursor.fetchall()]


class PostgresConnection:
    placeholder = "%s"

    def __init__(self, dsn: str) -> None:
        import psycopg  # noqa: PLC0415 -- optional dependency, imported only when used

        self._connection = psycopg.connect(dsn, autocommit=False)
        self._connection.read_only = True
        self._connection.execute("set statement_timeout = '60s'")

    def query(self, sql: str, params: Sequence[Any] = ()) -> Result:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                columns = [d.name for d in cursor.description] if cursor.description else []
                return columns, [tuple(row) for row in cursor.fetchall()]
        except Exception:
            # A failed statement aborts the Postgres transaction; without this
            # every check after a failing one would report "current transaction
            # is aborted" instead of its own result. Nothing is lost: the
            # session is read-only.
            self._connection.rollback()
            raise

    def close(self) -> None:
        self._connection.rollback()
        self._connection.close()


class SourceError(RuntimeError):
    """Could not open the source at all -- distinct from a failing check."""


@contextmanager
def connect(source: Source) -> Iterator[Connection]:
    if source.kind == "postgres":
        assert source.dsn_env is not None
        dsn = os.environ.get(source.dsn_env)
        if not dsn:
            raise SourceError(f"environment variable {source.dsn_env} is not set")
        try:
            pg = PostgresConnection(dsn)
        except Exception as error:  # psycopg raises its own hierarchy; the message is what matters
            raise SourceError(f"could not connect to Postgres: {error}") from error
        try:
            yield pg
        finally:
            pg.close()
        return

    assert source.path is not None
    if source.kind == "sqlite":
        if not source.path.is_file():
            raise SourceError(f"sqlite file not found: {source.path}")
        # mode=ro: the database is opened read-only at the file level.
        connection = sqlite3.connect(f"file:{source.path.as_posix()}?mode=ro", uri=True)
    else:
        if not source.path.is_dir():
            raise SourceError(f"csv directory not found: {source.path}")
        connection = sqlite3.connect(":memory:")
        load_csv_directory(connection, source.path)
    try:
        yield SqliteConnection(connection)
    finally:
        connection.close()


# -- CSV loading ---------------------------------------------------------------

_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def load_csv_directory(connection: sqlite3.Connection, directory: Path) -> list[str]:
    """Each `name.csv` becomes table `name`. Returns the table names loaded."""
    loaded: list[str] = []
    for file in sorted(directory.glob("*.csv")):
        table = file.stem
        if not IDENTIFIER.match(table):
            raise SourceError(f"{file.name}: file name must be a SQL identifier to become a table name")
        load_csv_file(connection, table, file)
        loaded.append(table)
    if not loaded:
        raise SourceError(f"no .csv files in {directory}")
    return loaded


def load_csv_file(connection: sqlite3.Connection, table: str, file: Path) -> None:
    with file.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as error:
            raise SourceError(f"{file.name}: empty file") from error
        rows = [tuple(row) for row in reader]

    for column in header:
        if not IDENTIFIER.match(column):
            raise SourceError(f"{file.name}: column {column!r} is not a SQL identifier")

    types = [_infer_type(i, rows) for i in range(len(header))]
    columns = ", ".join(f'"{name}" {kind}' for name, kind in zip(header, types, strict=True))
    connection.execute(f'create table "{table}" ({columns})')
    placeholders = ", ".join("?" for _ in header)
    connection.executemany(
        f'insert into "{table}" values ({placeholders})',
        (
            tuple(_coerce(value, kind) for value, kind in zip(_pad(row, len(header)), types, strict=True))
            for row in rows
        ),
    )
    connection.commit()


def _pad(row: tuple[str, ...], width: int) -> tuple[str, ...]:
    return row + ("",) * (width - len(row)) if len(row) < width else row[:width]


def _infer_type(index: int, rows: list[tuple[str, ...]]) -> str:
    """INTEGER if every non-empty value is an integer, REAL if numeric, else TEXT.

    Empty cells are NULL and do not vote. A column of all-empty cells is TEXT.
    """
    values = [row[index] for row in rows if index < len(row) and row[index] != ""]
    if not values:
        return "TEXT"
    if all(_INT.match(v) for v in values):
        return "INTEGER"
    if all(_FLOAT.match(v) for v in values):
        return "REAL"
    return "TEXT"


def _coerce(value: str, kind: str) -> int | float | str | None:
    if value == "":
        return None
    if kind == "INTEGER":
        return int(value)
    if kind == "REAL":
        return float(value)
    return value
