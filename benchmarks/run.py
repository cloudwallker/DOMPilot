"""Run four deterministic browser tasks against local pages.

Usage: python -m benchmarks.run [--mode fake|real] [--repeats 5]
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from dompilot.actions import RunConfig
from dompilot.agent import Agent
from tests.support import FakeModel, fixture_server


@dataclass(frozen=True)
class Task:
    page: str
    instruction: str
    verifier: object


def _search(page):
    return (
        page.locator("#query").input_value() == "Transformer"
        and page.locator('[role="status"]').inner_text() == "Search results: Transformer"
    )


def _form(page):
    return (
        page.locator("#name").input_value() == "Ada"
        and page.locator("#accept").is_checked()
        and page.locator('[role="status"]').inner_text() == "Submitted: Ada; accepted"
    )


def _dropdown(page):
    return (
        page.locator("#language").input_value() == "python"
        and page.locator('[role="status"]').inner_text() == "Selected: python"
    )


def _dynamic(page):
    return (
        page.locator("#dynamic-text").input_value() == "hello"
        and page.locator('[role="status"]').inner_text() == "Saved: hello"
    )


TASKS = (
    Task("search", "Search for Transformer and submit the search.", _search),
    Task("form", "Enter Ada as Name, accept the terms, and submit.", _form),
    Task("dropdown", "Select python as the Language and apply it.", _dropdown),
    Task("dynamic", "Reveal the editor, enter hello as Dynamic text, and save.", _dynamic),
)


def _verify(check):
    def verifier(page):
        try:
            return "passed" if check(page) else "failed"
        except Exception:
            return "failed"

    return verifier


def _metric(metrics, *names, default=0):
    for name in names:
        value = metrics.get(name)
        if isinstance(value, (int, float)):
            return value
    return default


def _summary(values):
    if not values:
        return "n/a"
    low, mid, high = min(values), median(values), max(values)
    return f"{mid:g} [{low:g}–{high:g}]"


def _make_model(mode):
    if mode == "fake":
        return FakeModel()
    from dompilot.model import OpenAIModel

    return OpenAIModel.from_env()


def _trace_input_chars(trace_path, fallback=0):
    try:
        trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
        return sum(step.get("input_chars", 0) for step in trace.get("steps", []))
    except (OSError, ValueError, TypeError):
        return fallback


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fake", "real"), default="fake")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--run-dir", type=Path, default=Path("runs") / "benchmark")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be positive")

    rows = []
    with fixture_server() as base_url:
        for task in TASKS:
            for repeat in range(1, args.repeats + 1):
                model = _make_model(args.mode)
                try:
                    config = RunConfig(headless=True, run_dir=args.run_dir)
                    result = Agent(model, config).run(
                        f"{base_url}{task.page}.html",
                        task.instruction,
                        _verify(task.verifier),
                    )
                finally:
                    close = getattr(model, "close", None)
                    if callable(close):
                        close()
                metrics = result.metrics or {}
                validation_failures = _metric(metrics, "validation_failures")
                action_failures = _metric(metrics, "action_failures")
                success = (
                    result.status == "verified_success"
                    and result.verification == "passed"
                    and validation_failures == 0
                    and action_failures == 0
                )
                row = {
                    "task": task.page,
                    "repeat": repeat,
                    "success": success,
                    "status": result.status,
                    "reason": result.reason,
                    "verification": result.verification,
                    "trace_path": result.trace_path,
                    "steps": _metric(metrics, "steps", "step_count"),
                    "decisions": _metric(metrics, "decisions"),
                    "validation_failures": validation_failures,
                    "action_failures": action_failures,
                    "actions": _metric(metrics, "browser_actions", "actions", "action_count"),
                    "calls": _metric(metrics, "api_calls"),
                    "input_chars": _trace_input_chars(
                        result.trace_path, getattr(model, "total_input_chars", 0)
                    ),
                    "latency": _metric(
                        metrics,
                        "elapsed_seconds",
                        "duration_seconds",
                        "wall_seconds",
                        "latency_seconds",
                    ),
                }
                rows.append(row)
                outcome = "PASS" if success else f"FAIL ({result.status}: {result.reason})"
                print(f"{task.page} {repeat}/{args.repeats}: {outcome}", flush=True)

    passed = sum(row["success"] for row in rows)
    valid_decisions = sum(row["decisions"] for row in rows)
    invalid_decisions = sum(row["validation_failures"] for row in rows)
    decision_attempts = valid_decisions + invalid_decisions
    invalid_rate = invalid_decisions / decision_attempts if decision_attempts else None
    aggregates = {
        "total": len(rows),
        "passed": passed,
        "success_rate": passed / len(rows),
        "model_decisions": valid_decisions,
        "invalid_decisions": invalid_decisions,
        "invalid_decision_rate": invalid_rate,
    }
    for key in ("steps", "actions", "calls", "input_chars", "latency"):
        values = [row[key] for row in rows]
        aggregates[key] = {"median": median(values), "min": min(values), "max": max(values)}
    args.run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {"mode": args.mode, "repeats": args.repeats, "aggregates": aggregates, "runs": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Success: {passed}/{len(rows)} ({passed / len(rows):.0%})")
    print(f"Model decisions: {valid_decisions}")
    print(
        "Invalid decision rate: " + (f"{invalid_rate:.1%}" if invalid_rate is not None else "n/a")
    )
    for label, key in (
        ("Steps", "steps"),
        ("Browser actions", "actions"),
        ("API calls", "calls"),
        ("Input characters", "input_chars"),
        ("Latency seconds", "latency"),
    ):
        print(f"{label}: median [min–max] = {_summary([row[key] for row in rows])}")
    print(f"Summary: {summary_path}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
