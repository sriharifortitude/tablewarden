"""Run every check in a config against its source and collect results."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .checks import Outcome, compile_check, interpret
from .config import Check, Config
from .sources import Connection, connect


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: Check
    outcome: Outcome
    duration_ms: float


@dataclass(frozen=True, slots=True)
class RunResult:
    started_at: datetime
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> int:
        return sum(r.outcome.status == "pass" for r in self.results)

    @property
    def failed(self) -> int:
        return sum(r.outcome.status == "fail" for r in self.results)

    @property
    def errored(self) -> int:
        return sum(r.outcome.status == "error" for r in self.results)

    def exit_code(self, fail_on: str = "error") -> int:
        """0 clean; 1 a check failed at or above the threshold; 2 a check could not run.

        2 wins over 1: a check that could not run is a bigger problem than one that
        ran and failed, and a CI gate should not report "data bad" when the truth
        is "could not look".
        """
        if self.errored:
            return 2
        threshold = {"error": ("error",), "warn": ("error", "warn")}[fail_on]
        if any(r.outcome.status == "fail" and r.check.severity in threshold for r in self.results):
            return 1
        return 0


def run(config: Config, now: datetime | None = None) -> RunResult:
    started = now or datetime.now(UTC)
    with connect(config.source) as connection:
        results = tuple(run_check(connection, check, started) for check in config.checks)
    return RunResult(started_at=started, results=results)


def run_check(connection: Connection, check: Check, now: datetime | None = None) -> CheckResult:
    compiled = compile_check(check)
    start = time.perf_counter()
    try:
        _, rows = connection.query(compiled.measure.for_placeholder(connection.placeholder), compiled.measure.params)
        outcome = interpret(check, rows, now)
        if outcome.status == "fail" and compiled.sample is not None:
            columns, sample = connection.query(
                compiled.sample.for_placeholder(connection.placeholder), compiled.sample.params
            )
            outcome = Outcome(
                outcome.status, outcome.observed, outcome.failures,
                tuple(sample), tuple(columns), outcome.message, outcome.extra,
            )  # fmt: skip
    except Exception as error:
        outcome = Outcome("error", "could not run", message=f"{type(error).__name__}: {error}")
    return CheckResult(check=check, outcome=outcome, duration_ms=(time.perf_counter() - start) * 1000)
