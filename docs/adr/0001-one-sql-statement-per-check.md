# 1. Every check is one SQL statement, and CSV becomes SQLite

Status: accepted — 2026-09-18

## Context

A data-quality tool can pull rows into Python and inspect them, or push
the question to the database. Pulling is flexible (any Python predicate)
and does not scale: a not-null check on a 50-million-row table should not
transfer 50 million rows. Pushing means every check has to be expressible
in SQL that both Postgres and SQLite accept.

CSV support is expected of such a tool, and it is where the two designs
diverge most: a CSV has no query engine.

## Decision

- Each check compiles to one *measure* statement whose first row decides
  the outcome, plus an optional *sample* statement run only on failure.
  The SQL uses only constructs common to Postgres and SQLite: `count`,
  `group by … having`, `not in (subquery)`, `case when`, `max`.
- Driver differences are confined to the parameter placeholder (`?` vs
  `%s`) and to how timestamps come back (text vs `datetime`), both handled
  at the boundary.
- CSV files are loaded into an in-memory SQLite database with three-way
  type inference (INTEGER, REAL, TEXT; empty is NULL). After that a CSV is
  a SQLite source and gets every check for free.

## Consequences

- Row transfer is bounded by the sample size, never by the table size.
- A new check kind is a new `case` in `compile_check` and one in
  `interpret`, plus tests. There is no per-engine code to add.
- The Python side never sees the data, which is also the privacy
  property: the tool reports counts and small samples, and samples can be
  turned off (`sample = 0`) for tables with personal data.
- Type inference for CSV is deliberately simple. A column of ZIP codes
  with a leading zero becomes INTEGER and loses the zero, which matters
  for `accepted_values` against string values. The README says so; a
  per-column type override in the config would be the fix if it bites.
- `not in (subquery)` for `references` is portable but not the fastest
  formulation on Postgres for very large parents; a `left join … is null`
  would be, and is a one-line change confined to `compile_check`.
