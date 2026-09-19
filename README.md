# tablewarden

Data-quality checks for Postgres, SQLite and CSV files, declared in a
TOML file and run as a CI gate. Eight kinds of check, each compiled to one
SQL statement; failing rows are sampled into the report; exit codes and a
JUnit file are what your pipeline reads.

    pip install "tablewarden[postgres] @ git+https://github.com/sriharifortitude/tablewarden@v0.1.0"
    tablewarden run checks.toml --format junit --output report.xml

Not on PyPI yet; the line above installs the tagged release from GitHub.

```toml
[source]
kind = "postgres"
dsn_env = "DATABASE_URL"          # the DSN lives in the environment, not here

[[check]]
kind = "not_null"
table = "orders"
column = "customer_id"

[[check]]
kind = "unique"
table = "order_lines"
columns = ["order_id", "sku"]

[[check]]
kind = "references"
table = "order_lines"
column = "order_id"
to_table = "orders"
to_column = "id"

[[check]]
kind = "freshness"
table = "orders"
column = "created_at"
max_age = "2h"
severity = "warn"

[[check]]
kind = "sql"
name = "orders.no_negative_totals"
query = "select count(*) from orders where total < 0"
expect = 0
```

```
FAIL  orders.not_null.customer_email  1 row  (0 ms)
        sample (id, customer_email, status, total, created_at):
          2 | NULL | paid | 20.0 | 2026-09-17T09:00:00
FAIL  order_lines.references.order_id  1 orphan row  (0 ms)
        sample (order_id, n):
          9 | 1
ok    orders.row_count  6 rows, expected within [1, 1000]  (0 ms)
warn  orders.freshness.created_at  max(created_at) is 6h 19m old, limit 2h  (0 ms)

10 checks: 2 passed, 8 failed, 0 errored
```

That output is `tablewarden run examples/shop.toml`, against the CSV
files in `examples/shop/`, which carry one of every defect on purpose.

## Checks

| kind | fails when | extra keys |
| --- | --- | --- |
| `not_null` | any row has a NULL in `column` | |
| `unique` | any combination of `columns` appears more than once | `columns` or `column` |
| `accepted_values` | a non-NULL `column` value is not in `values` | `values` |
| `null_rate` | NULLs in `column` exceed `max_rate` of rows | `max_rate` (0–1) |
| `row_count` | the count is outside `min`..`max` | `min`, `max` |
| `references` | a non-NULL `column` has no match in `to_table.to_column` | `to_table`, `to_column` |
| `freshness` | `max(column)` is older than `max_age` (`30m`, `2h`, `1d`) | `max_age` |
| `sql` | the query's first value is not `expect`, or exceeds `expect_max` | `query`, `expect` / `expect_max` |

Every check takes `name` (defaults to `table.kind.column`), `severity`
(`error` or `warn`), `where` (a SQL boolean expression appended to the
statement) and `sample` (how many offending rows to show, 0–100).

## Sources

- **postgres** — `dsn_env` names the environment variable. The session is
  opened read-only and with a 60 s statement timeout; a `sql` check that
  tries to write gets an error, not a write.
- **sqlite** — `path` to the file, opened read-only at the file level.
- **csv** — `path` to a directory; each `name.csv` becomes table `name`.
  Columns whose non-empty values are all integers become INTEGER, all
  numeric become REAL, otherwise TEXT; empty cells are NULL. Everything
  then runs as SQLite, so CSVs get every check without a second engine.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | every check passed, or only `warn`-severity checks failed |
| 1 | an `error`-severity check failed (`--fail-on warn` includes warnings) |
| 2 | a check could not run, or the config or source could not be opened |

2 outranks 1: "could not look" is a worse state for a gate than "looked
and found problems", and should not be reported as the latter.

## Reports

`--format terminal` (default), `json`, or `junit`. With `--output FILE`
the report is written to the file and the terminal summary still prints.
In JUnit, a failed `error`-severity check is a `<failure>`, a check that
could not run is an `<error>`, and a failed `warn`-severity check is
`<skipped>` with the observation as its message — the closest JUnit has to
yellow.

## The config is code

`where` clauses and `sql` queries are SQL you wrote, spliced into the
statement as written. Table and column names are restricted to
`[A-Za-z0-9_]` and quoted; every value (`values`, thresholds) is a bound
parameter. Treat the TOML file the way you treat a migration: reviewed,
in version control, run with a read-only credential.

## Development

    python -m venv .venv && .venv/bin/pip install -e ".[dev]"
    ruff check src tests && ruff format --check src tests
    mypy
    pytest                                  # 39 tests, no database needed
    TABLEWARDEN_TEST_DSN=postgresql://... pytest tests/test_postgres.py

Design notes are in [docs/adr/](docs/adr/).

## What it deliberately does not do

- **No MySQL, no Parquet, no cloud warehouses.** The SQL is standard
  enough that a MySQL source would be a placeholder mapping and quoting
  rule; Parquet would be a DuckDB source. Neither is here yet.
- **No scheduling, no history, no alerting.** It runs, reports, exits.
  Run it from cron or CI; keep the JSON if you want a history.
- **No cross-column or statistical checks** (distribution drift, outlier
  detection). Those need a baseline, which needs history.
- **Freshness assumes UTC** for naive timestamps. SQLite has no timestamp
  type; a `text` column in local time will read as UTC and be off by the
  offset. Store UTC or use Postgres `timestamptz`.
- **`references` with NULL-heavy parents.** The parent subquery excludes
  NULLs so that `not in` behaves; a parent column that is not the key of
  its table still works but will be slow without an index.

## Licence

MIT.
