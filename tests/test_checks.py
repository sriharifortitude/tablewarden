from datetime import UTC, datetime

import pytest

from tablewarden.checks import Statement, compile_check, interpret
from tablewarden.config import Check


def test_not_null_with_where_and_sample() -> None:
    c = compile_check(Check("not_null", "n", table="orders", column="email", where="status = 'paid'", sample=3))
    assert c.measure == Statement('select count(*) from "orders" where "email" is null and (status = \'paid\')')
    assert c.sample == Statement('select * from "orders" where "email" is null and (status = \'paid\') limit 3')


def test_unique_over_two_columns() -> None:
    c = compile_check(Check("unique", "n", table="lines", columns=("order_id", "sku")))
    inner = 'select "order_id", "sku", count(*) as n from "lines" group by "order_id", "sku" having count(*) > 1'
    assert c.measure.sql == f"select count(*) from ({inner}) as duplicated"
    assert c.sample is not None and c.sample.sql == f"{inner} order by n desc limit 5"


def test_accepted_values_binds_values_and_ignores_nulls() -> None:
    c = compile_check(Check("accepted_values", "n", table="orders", column="status", values=("new", "paid", 3)))
    assert c.measure == Statement(
        'select count(*) from "orders" where "status" is not null and "status" not in (?, ?, ?)', ("new", "paid", 3)
    )
    assert c.measure.for_placeholder("%s") == (
        'select count(*) from "orders" where "status" is not null and "status" not in (%s, %s, %s)'
    )


def test_references_excludes_null_parents_from_the_subquery() -> None:
    c = compile_check(Check("references", "n", table="lines", column="order_id", to_table="orders", to_column="id"))
    assert c.measure.sql == (
        'select count(*) from "lines" where "order_id" is not null '
        'and "order_id" not in (select "id" from "orders" where "id" is not null)'
    )


def test_sample_zero_disables_the_sample_statement() -> None:
    assert compile_check(Check("not_null", "n", table="t", column="c", sample=0)).sample is None


def test_sql_check_runs_the_query_verbatim() -> None:
    assert (
        compile_check(Check("sql", "n", query="select count(*) from x", expect=0)).measure.sql
        == "select count(*) from x"
    )


def test_interpret_counts() -> None:
    check = Check("unique", "n", table="t", columns=("a",))
    assert interpret(check, [(0,)]).status == "pass"
    failed = interpret(check, [(2,)])
    assert (failed.status, failed.observed, failed.failures) == ("fail", "2 duplicated keys", 2)
    assert interpret(Check("references", "n"), [(1,)]).observed == "1 orphan row"


def test_interpret_null_rate_boundaries() -> None:
    check = Check("null_rate", "n", table="t", column="c", max_rate=0.05)
    assert interpret(check, [(100, 5)]).status == "pass"  # exactly the limit passes
    failed = interpret(check, [(100, 6)])
    assert failed.status == "fail" and failed.observed == "rate 0.0600 (6 of 100) > 0.05"
    assert interpret(check, [(0, None)]).observed == "rate 0.0000 (0 of 0) <= 0.05"


def test_interpret_row_count_bounds() -> None:
    assert interpret(Check("row_count", "n", table="t", min_rows=1), [(0,)]).status == "fail"
    assert interpret(Check("row_count", "n", table="t", min_rows=1, max_rows=10), [(10,)]).status == "pass"
    out = interpret(Check("row_count", "n", table="t", max_rows=10), [(11,)])
    assert out.observed == "11 rows, expected within [, 10]"


def test_interpret_freshness_from_text_and_datetime() -> None:
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    check = Check("freshness", "n", table="t", column="ts", max_age_seconds=3600)
    ok = interpret(check, [("2026-09-18 11:30:00",)], now)  # SQLite text, naive => UTC
    assert ok.status == "pass" and ok.observed == "max(ts) is 30m old, limit 1h"
    late = interpret(check, [(datetime(2026, 9, 18, 9, 47, tzinfo=UTC),)], now)
    assert late.status == "fail" and late.observed == "max(ts) is 2h 13m old, limit 1h"
    assert interpret(check, [(None,)], now).observed == "no non-null ts values"
    with pytest.raises(TypeError, match="freshness column must be a timestamp"):
        interpret(check, [(42,)], now)


def test_interpret_sql_expectations() -> None:
    assert interpret(Check("sql", "n", query="q", expect=0), [(0,)]).status == "pass"
    assert interpret(Check("sql", "n", query="q", expect=0), [(3,)]).observed == "3 != 0"
    assert interpret(Check("sql", "n", query="q", expect_max=5), [(5,)]).status == "pass"
    assert interpret(Check("sql", "n", query="q", expect_max=5), [(6,)]).observed == "6 > 5"
    assert interpret(Check("sql", "n", query="q", expect=0), []).status == "error"
