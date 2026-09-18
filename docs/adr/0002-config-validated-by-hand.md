# 2. TOML config, validated by hand, no schema library

Status: accepted — 2026-09-18

## Context

The config is the whole user interface. The two conventional choices are
YAML with a schema library (pydantic, jsonschema) or a Python file. The
first adds two dependencies and produces error messages in the library's
voice; the second makes the config Turing-complete and hard to review.

## Decision

- TOML, parsed by `tomllib` from the standard library. No YAML: TOML has
  no implicit typing surprises (`no` is a string, `1.0` is a float) and
  `[[check]]` arrays read well for a list of rules.
- Validation is a function per table, written by hand, that raises
  `ConfigError` with a message beginning at the path that is wrong
  (`check[3].max_age: expected a duration like 30m, 2h or 1d`). Unknown
  keys are errors, so a typo in `colum` cannot silently disable a check.
- The DSN is not permitted in the file. `source.dsn` is rejected with a
  message that names `dsn_env`.
- Check names default to `table.kind.column` and must be unique: they are
  the JUnit test-case names and the thing a person greps for.

## Consequences

- Zero runtime dependencies for the SQLite and CSV paths; `psycopg` is an
  extra.
- Every validation rule has a test in `test_config.py` asserting the
  exact message prefix, which is the contract. Twenty-odd rules is the
  size at which hand-written validation is still cheaper than a schema
  library plus the work of making its messages readable.
- `where` and `sql` are strings passed to the database as written. The
  config is code and the README says to treat it as such. Restricting
  them further (a SQL subset parser) was rejected: the tool's job is to
  ask questions the user wants asked, and the read-only session is the
  safeguard that matters.
- Durations accept a single unit (`90m`, not `1h30m`). Compound durations
  would be easy; the restriction is to keep the grammar in one regex a
  reader can verify.
