"""End to end against the example CSVs (loaded into SQLite) and a real SQLite file.

Every expected number was worked out from examples/shop/*.csv by hand:
  orders: 6 rows; row 2 has no email; id 5 appears twice; row 4 is
  'refunded' with total -3.00; rows 5 and 6 are 'new'.
  order_lines: 6 rows; order_id 9 has no parent; (3, A) appears twice.
"""

import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tablewarden import report
from tablewarden.cli import main
from tablewarden.config import load
from tablewarden.runner import RunResult, run
from tablewarden.sources import SourceError, load_csv_file

EXAMPLE = Path(__file__).parent.parent / "examples" / "shop.toml"
NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)  # 1h 49m after the newest order


def statuses(result: RunResult) -> dict[str, str]:
    return {r.check.name: r.outcome.status for r in result.results}


def test_example_config_against_csv() -> None:
    result = run(load(EXAMPLE), now=NOW)
    assert statuses(result) == {
        "orders.not_null.customer_email": "fail",
        "orders.unique.id": "fail",
        "orders.accepted_values.status": "fail",
        "orders.null_rate.customer_email": "pass",  # 1 of 6 = 0.1667 <= 0.2
        "orders.row_count": "pass",
        "order_lines.references.order_id": "fail",
        "order_lines.unique.order_id.sku": "fail",
        "orders.freshness.created_at": "pass",  # 1h 49m old at NOW, limit 2h
        "orders.no_negative_totals": "fail",
        "orders.few_unprocessed": "fail",  # 2 'new' orders > 1, severity warn
    }
    by_name = {r.check.name: r for r in result.results}

    assert by_name["orders.not_null.customer_email"].outcome.observed == "1 row"
    assert by_name["orders.not_null.customer_email"].outcome.sample_columns == (
        "id",
        "customer_email",
        "status",
        "total",
        "created_at",
    )
    assert by_name["orders.not_null.customer_email"].outcome.sample == ((2, None, "paid", 20.0, "2026-09-17T09:00:00"),)

    assert by_name["orders.unique.id"].outcome.sample == ((5, 2),)
    assert by_name["orders.accepted_values.status"].outcome.sample == (("refunded", 1),)
    assert by_name["order_lines.references.order_id"].outcome.observed == "1 orphan row"
    assert by_name["order_lines.references.order_id"].outcome.sample == ((9, 1),)
    assert by_name["order_lines.unique.order_id.sku"].outcome.sample == ((3, "A", 2),)
    assert by_name["orders.freshness.created_at"].outcome.observed == "max(created_at) is 1h 49m old, limit 2h"
    assert by_name["orders.no_negative_totals"].outcome.observed == "1 != 0"
    assert by_name["orders.few_unprocessed"].outcome.observed == "2 > 1"

    assert (result.passed, result.failed, result.errored) == (3, 7, 0)
    assert result.exit_code("error") == 1
    assert result.exit_code("warn") == 1


def test_csv_type_inference(tmp_path: Path) -> None:
    (tmp_path / "t.csv").write_text("i,f,s,e\n1,1.5,x,\n2,2,y,\n,3.25,3,\n", encoding="utf-8")
    connection = sqlite3.connect(":memory:")
    load_csv_file(connection, "t", tmp_path / "t.csv")
    types = {row[1]: row[2] for row in connection.execute("pragma table_info(t)")}
    assert types == {"i": "INTEGER", "f": "REAL", "s": "TEXT", "e": "TEXT"}
    assert connection.execute("select i, f, s, e from t order by rowid").fetchall() == [
        (1, 1.5, "x", None),
        (2, 2.0, "y", None),
        (None, 3.25, "3", None),
    ]


def test_csv_directory_rejects_non_identifier_file_names(tmp_path: Path) -> None:
    (tmp_path / "bad-name.csv").write_text("a\n1\n")
    with pytest.raises(SourceError, match=r"bad-name.csv: file name must be a SQL identifier"):
        run(load(_write_config(tmp_path, 'kind = "csv"\npath = "."')))


def test_sqlite_file_is_opened_read_only_and_errors_are_per_check(tmp_path: Path) -> None:
    db = tmp_path / "shop.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("create table t (id integer, ts text)")
    connection.execute("insert into t values (1, '2026-09-18 09:00:00'), (2, null)")
    connection.commit()
    connection.close()

    config = load(
        _write_config(
            tmp_path,
            'kind = "sqlite"\npath = "shop.sqlite"',
            """
[[check]]
kind = "not_null"
table = "t"
column = "ts"

[[check]]
kind = "sql"
name = "tries_to_write"
query = "delete from t"
expect = 0

[[check]]
kind = "row_count"
table = "missing"
min = 1
""",
        )
    )
    result = run(config, now=NOW)
    by_name = {r.check.name: r for r in result.results}
    assert by_name["t.not_null.ts"].outcome.status == "fail"
    assert by_name["tries_to_write"].outcome.status == "error"
    assert "readonly" in by_name["tries_to_write"].outcome.message
    assert by_name["missing.row_count"].outcome.status == "error"
    assert "no such table" in by_name["missing.row_count"].outcome.message
    assert result.exit_code() == 2  # errors outrank failures


def test_reports(tmp_path: Path) -> None:
    result = run(load(EXAMPLE), now=NOW)

    text = report.terminal(result)
    assert "FAIL  orders.unique.id  1 duplicated key" in text
    assert "warn  orders.few_unprocessed  2 > 1" in text
    assert "ok    orders.row_count  6 rows, expected within [1, 1000]" in text
    assert text.rstrip().endswith("10 checks: 3 passed, 7 failed, 0 errored")

    data = json.loads(report.as_json(result))
    assert data["summary"] == {"total": 10, "passed": 3, "failed": 7, "errored": 0}
    nn = next(c for c in data["checks"] if c["name"] == "orders.not_null.customer_email")
    assert nn["sample"] == {
        "columns": ["id", "customer_email", "status", "total", "created_at"],
        "rows": [[2, None, "paid", 20.0, "2026-09-17T09:00:00"]],
    }
    rate = next(c for c in data["checks"] if c["name"] == "orders.null_rate.customer_email")
    assert rate["nulls"] == 1 and rate["total"] == 6

    xml = report.junit(result)
    suite = ET.fromstring(xml)
    assert suite.attrib["tests"] == "10"
    assert suite.attrib["failures"] == "6"  # 7 fails, 1 of them warn-severity (freshness passes at NOW)
    assert suite.attrib["skipped"] == "1"
    assert suite.attrib["errors"] == "0"
    cases = {c.attrib["name"]: c for c in suite}
    assert cases["orders.row_count"].find("failure") is None
    failure = cases["orders.unique.id"].find("failure")
    skipped = cases["orders.few_unprocessed"].find("skipped")
    assert failure is not None and failure.attrib["message"] == "1 duplicated key"
    assert skipped is not None and skipped.attrib["message"] == "2 > 1"


def test_cli_exit_codes_and_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLE)]) == 0
    assert "10 checks, source csv" in capsys.readouterr().out

    assert main(["run", str(EXAMPLE), "--format", "junit", "--output", str(tmp_path / "r.xml")]) == 1
    assert (tmp_path / "r.xml").read_text().startswith('<?xml version="1.0"')
    assert "checks:" in capsys.readouterr().out  # terminal summary still printed

    assert main(["run", str(tmp_path / "nope.toml")]) == 2
    assert "config error" in capsys.readouterr().err

    missing = _write_config(tmp_path, 'kind = "sqlite"\npath = "absent.sqlite"')
    assert main(["run", str(missing)]) == 2
    assert "source error: sqlite file not found" in capsys.readouterr().err


def _write_config(directory: Path, source: str, checks: str | None = None) -> Path:
    path = directory / "checks.toml"
    body = checks or '\n[[check]]\nkind = "row_count"\ntable = "t"\nmin = 1\n'
    path.write_text(f"[source]\n{source}\n{body}", encoding="utf-8")
    return path
