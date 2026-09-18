"""Three renderings of a run: terminal, JSON, JUnit XML.

JUnit is what CI systems already know how to display and gate on; the
mapping is one testsuite, one testcase per check, `failure` for a failed
check and `error` for one that could not run. A `warn`-severity failure
is reported as skipped-with-message so the suite stays green but the
message is visible -- the closest JUnit gets to "yellow".
"""

from __future__ import annotations

import json
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .runner import CheckResult, RunResult

_TICK = {"pass": "ok  ", "fail": "FAIL", "error": "ERR "}


def terminal(run: RunResult, *, color: bool = False) -> str:
    lines: list[str] = []
    for result in run.results:
        status = result.outcome.status
        label = _TICK[status]
        if status == "fail" and result.check.severity == "warn":
            label = "warn"
        if color:
            label = _paint(label, status, result.check.severity)
        lines.append(f"{label}  {result.check.name}  {result.outcome.observed}  ({result.duration_ms:.0f} ms)")
        if result.outcome.message:
            lines.append(f"        {result.outcome.message}")
        if result.outcome.sample:
            lines.append(f"        sample ({', '.join(result.outcome.sample_columns)}):")
            lines.extend(f"          {_row(row)}" for row in result.outcome.sample)
    lines.append("")
    lines.append(f"{len(run.results)} checks: {run.passed} passed, {run.failed} failed, {run.errored} errored")
    return "\n".join(lines) + "\n"


def _paint(label: str, status: str, severity: str) -> str:
    code = {"pass": "32", "fail": "33" if severity == "warn" else "31", "error": "35"}[status]
    return f"\x1b[{code}m{label}\x1b[0m"


def _row(row: tuple[Any, ...]) -> str:
    return " | ".join("NULL" if v is None else str(v) for v in row)


def as_json(run: RunResult) -> str:
    return json.dumps(
        {
            "started_at": run.started_at.isoformat(),
            "summary": {
                "total": len(run.results),
                "passed": run.passed,
                "failed": run.failed,
                "errored": run.errored,
            },
            "checks": [_result_json(r) for r in run.results],
        },
        indent=2,
        default=str,
    )


def _result_json(result: CheckResult) -> dict[str, Any]:
    check = result.check
    return {
        "name": check.name,
        "kind": check.kind,
        "severity": check.severity,
        "table": check.table,
        "status": result.outcome.status,
        "observed": result.outcome.observed,
        "failures": result.outcome.failures,
        "message": result.outcome.message or None,
        "sample": {
            "columns": list(result.outcome.sample_columns),
            "rows": [list(r) for r in result.outcome.sample],
        }
        if result.outcome.sample
        else None,
        "duration_ms": round(result.duration_ms, 1),
        **result.outcome.extra,
    }


def junit(run: RunResult, suite_name: str = "tablewarden") -> str:
    cases: list[str] = []
    for result in run.results:
        check, outcome = result.check, result.outcome
        seconds = f"{result.duration_ms / 1000:.3f}"
        attrs = (
            f"name={quoteattr(check.name)} classname={quoteattr(check.table or check.kind)} time={quoteattr(seconds)}"
        )
        if outcome.status == "pass":
            cases.append(f"  <testcase {attrs}/>")
            continue
        body = escape(outcome.message or outcome.observed)
        if outcome.sample:
            body += "\n" + "\n".join(_row(r) for r in outcome.sample)
        if outcome.status == "error":
            tag = "error"
        elif check.severity == "warn":
            tag = "skipped"
        else:
            tag = "failure"
        cases.append(
            f"  <testcase {attrs}>\n    <{tag} message={quoteattr(outcome.observed)}>{body}</{tag}>\n  </testcase>"
        )
    warned = sum(r.outcome.status == "fail" and r.check.severity == "warn" for r in run.results)
    header = (
        f'<testsuite name={quoteattr(suite_name)} tests="{len(run.results)}" failures="{run.failed - warned}" '
        f'errors="{run.errored}" skipped="{warned}" timestamp={quoteattr(run.started_at.isoformat())}>'
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + header + "\n" + "\n".join(cases) + "\n</testsuite>\n"
