"""Against a real Postgres. Skipped unless TABLEWARDEN_TEST_DSN points at a scratch database.

What SQLite cannot prove: the %s placeholder path, real timestamptz values
in freshness, and that the session really is read-only.
"""

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tablewarden.config import parse
from tablewarden.runner import run

DSN = os.environ.get("TABLEWARDEN_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TABLEWARDEN_TEST_DSN not set")


@pytest.fixture(scope="module")
def schema() -> None:
    with psycopg.connect(DSN or "") as connection:
        connection.execute("drop table if exists tw_lines; drop table if exists tw_orders")
        connection.execute("create table tw_orders (id int primary key, status text, created_at timestamptz)")
        connection.execute("create table tw_lines (order_id int, sku text)")
        connection.execute(
            "insert into tw_orders values "
            "(1, 'paid', now() - interval '10 minutes'), (2, 'odd', now() - interval '3 hours')"
        )
        connection.execute("insert into tw_lines values (1, 'A'), (7, 'B'), (1, 'A')")
        connection.commit()


def test_checks_against_postgres(schema: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TW_DSN", DSN or "")
    config = parse(
        {
            "source": {"kind": "postgres", "dsn_env": "TW_DSN"},
            "check": [
                {"kind": "accepted_values", "table": "tw_orders", "column": "status", "values": ["paid", "new"]},
                {
                    "kind": "references",
                    "table": "tw_lines",
                    "column": "order_id",
                    "to_table": "tw_orders",
                    "to_column": "id",
                },
                {"kind": "unique", "table": "tw_lines", "columns": ["order_id", "sku"]},
                {"kind": "freshness", "table": "tw_orders", "column": "created_at", "max_age": "30m"},
                {"kind": "freshness", "table": "tw_orders", "column": "created_at", "max_age": "5m", "name": "strict"},
                {"kind": "sql", "name": "writes_are_refused", "query": "delete from tw_lines", "expect": 0},
                {"kind": "row_count", "table": "tw_orders", "min": 2, "max": 2},
            ],
        }
    )
    result = run(config, now=datetime.now(UTC))
    by_name = {r.check.name: r for r in result.results}

    assert by_name["tw_orders.accepted_values.status"].outcome.sample == (("odd", 1),)
    assert by_name["tw_lines.references.order_id"].outcome.sample == ((7, 1),)
    assert by_name["tw_lines.unique.order_id.sku"].outcome.sample == ((1, "A", 2),)
    assert by_name["tw_orders.freshness.created_at"].outcome.status == "pass"
    assert by_name["strict"].outcome.status == "fail"
    assert by_name["strict"].outcome.extra["age_seconds"] > timedelta(minutes=9).total_seconds()
    assert by_name["writes_are_refused"].outcome.status == "error"
    assert "read-only" in by_name["writes_are_refused"].outcome.message
    assert by_name["tw_orders.row_count"].outcome.status == "pass"
