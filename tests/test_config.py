from pathlib import Path
from typing import Any

import pytest

from tablewarden.config import ConfigError, load, parse

SOURCE = {"source": {"kind": "sqlite", "path": "db.sqlite"}}


def cfg(*checks: dict[str, Any]) -> dict[str, Any]:
    return {**SOURCE, "check": list(checks)}


def test_source_paths_resolve_relative_to_the_config_file(tmp_path: Path) -> None:
    (tmp_path / "checks.toml").write_text(
        '[source]\nkind = "csv"\npath = "data"\n\n[[check]]\nkind = "row_count"\ntable = "t"\nmin = 1\n'
    )
    config = load(tmp_path / "checks.toml")
    assert config.source.path == tmp_path / "data"
    assert config.checks[0].name == "t.row_count"


def test_dsn_is_refused_in_the_file() -> None:
    with pytest.raises(ConfigError, match=r"^source\.dsn: connection strings do not belong"):
        parse({"source": {"kind": "postgres", "dsn": "postgresql://x", "dsn_env": "X"}, "check": [{"kind": "sql"}]})


def test_default_names_and_duplicate_detection() -> None:
    config = parse(
        cfg(
            {"kind": "not_null", "table": "orders", "column": "id"},
            {"kind": "unique", "table": "orders", "columns": ["a", "b"]},
            {"kind": "sql", "query": "select 1", "expect": 1},
        )
    )
    assert [c.name for c in config.checks] == ["orders.not_null.id", "orders.unique.a.b", "sql"]
    with pytest.raises(ConfigError, match=r"duplicate names \['orders.not_null.id'\]"):
        parse(
            cfg(
                {"kind": "not_null", "table": "orders", "column": "id"},
                {"kind": "not_null", "table": "orders", "column": "id"},
            )
        )


@pytest.mark.parametrize(
    ("check", "message"),
    [
        ({"kind": "nope"}, r"^check\[0\]\.kind: expected one of"),
        ({"kind": "not_null", "column": "c"}, r"^check\[0\]\.table: required for not_null$"),
        ({"kind": "not_null", "table": "t"}, r"^check\[0\]\.column: required for not_null$"),
        (
            {"kind": "not_null", "table": "t; drop", "column": "c"},
            r"^check\[0\]\.table: expected an unquoted SQL identifier",
        ),
        ({"kind": "unique", "table": "t"}, r"^check\[0\]\.columns: required for unique"),
        (
            {"kind": "accepted_values", "table": "t", "column": "c", "values": []},
            r"^check\[0\]\.values: expected a non-empty list",
        ),
        (
            {"kind": "null_rate", "table": "t", "column": "c", "max_rate": 1.5},
            r"^check\[0\]\.max_rate: expected a number between 0 and 1",
        ),
        ({"kind": "row_count", "table": "t"}, r"^check\[0\]: row_count needs min, max or both"),
        ({"kind": "row_count", "table": "t", "min": -1}, r"^check\[0\]\.min: expected a non-negative integer"),
        (
            {"kind": "references", "table": "t", "column": "c", "to_table": "p"},
            r"^check\[0\]: references needs to_table and to_column",
        ),
        (
            {"kind": "freshness", "table": "t", "column": "c", "max_age": "2 weeks"},
            r"^check\[0\]\.max_age: expected a duration like",
        ),
        ({"kind": "sql", "query": "select 1"}, r"^check\[0\]: give exactly one of expect or expect_max"),
        (
            {"kind": "sql", "query": "select 1", "expect": 1, "expect_max": 2},
            r"^check\[0\]: give exactly one of expect or expect_max",
        ),
        ({"kind": "sql", "query": "select 1", "expect": True}, r"^check\[0\]\.expect: expected an integer"),
        (
            {"kind": "row_count", "table": "t", "min": 1, "severity": "fatal"},
            r"^check\[0\]\.severity: expected error or warn",
        ),
        (
            {"kind": "row_count", "table": "t", "min": 1, "sample": 101},
            r"^check\[0\]\.sample: expected an integer 0\.\.100",
        ),
        ({"kind": "row_count", "table": "t", "min": 1, "colour": "red"}, r"^check\[0\]: unknown keys \['colour'\]"),
    ],
)
def test_each_error_names_the_path_and_the_expectation(check: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(cfg(check))


def test_durations_and_unique_single_column() -> None:
    config = parse(
        cfg(
            {"kind": "freshness", "table": "t", "column": "ts", "max_age": "90m"},
            {"kind": "freshness", "table": "t", "column": "ts", "max_age": "2d", "name": "two-days"},
            {"kind": "unique", "table": "t", "column": "id"},
        )
    )
    assert config.checks[0].max_age_seconds == 5400
    assert config.checks[1].max_age_seconds == 172_800
    assert config.checks[2].columns == ("id",)


def test_invalid_toml_reports_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[source\n")
    with pytest.raises(ConfigError, match=r"bad\.toml: not valid TOML"):
        load(bad)
