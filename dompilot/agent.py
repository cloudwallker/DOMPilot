"""One synchronous observe–decide–act loop with bounded recovery."""

import json
import os
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from platform import python_version
from uuid import uuid4

from .actions import (
    DecisionInput,
    ModelReply,
    RunConfig,
    RunResult,
    action_key,
    build_action_space,
    compact_json,
    progress_key,
    validate_decision,
)
from .browser import BrowserSession
from .metrics import RunMetrics
from .model import ModelError, prepare_input, request_body


def _dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _action_name(decision) -> str:
    action = decision.decision.action
    return str(getattr(action, "value", action))


def _short_history(steps: list[dict]) -> list[dict]:
    history = []
    for step in steps[-5:]:
        item = {
            "action": step.get("action"),
            "success": step.get("result", {}).get("success")
            if isinstance(step.get("result"), dict)
            else None,
        }
        if step.get("error"):
            item["error"] = str(step["error"])[:160]
        history.append(item)
    return history


class Agent:
    def __init__(self, model, config: RunConfig | None = None, browser_factory=BrowserSession):
        self.model = model
        self.config = config or RunConfig()
        self.browser_factory = browser_factory

    def run(self, url: str, task: str, verifier=None) -> RunResult:
        config = self.config
        started = time.monotonic()
        deadline = started + config.max_seconds
        run_dir = Path(config.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:12]}"
        trace_path = run_dir / f"{run_id}.json"
        metrics = RunMetrics()
        metadata = getattr(self.model, "metadata", None)
        safe_metadata = (
            {key: metadata[key] for key in ("provider", "model", "output_mode") if key in metadata}
            if isinstance(metadata, dict)
            else {}
        )
        trace = {
            "trace_version": 1,
            "software": {
                "python": python_version(),
                **{
                    package: version(package)
                    for package in ("dompilot", "playwright", "pydantic", "openai")
                },
            },
            "url": url,
            "task": task[:2000],
            "config": config.model_dump(mode="json"),
            "model": safe_metadata,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "navigation": None,
            "steps": [],
            "status": "running",
            "reason": "",
            "verification": "unknown",
            "metrics": {},
        }
        status, reason, verification = "error", "unknown_error", "unknown"
        browser = None

        def persist() -> None:
            trace["metrics"] = metrics.as_dict(time.monotonic() - started)
            temporary = trace_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(trace, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            os.replace(temporary, trace_path)

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def observe():
            before = time.monotonic()
            observed = browser.observe()
            latency = time.monotonic() - before
            metrics.observe_latency += latency
            return observed, latency

        try:
            if not task.strip() or len(task) > 2000:
                raise ValueError("task_must_have_1_to_2000_characters")
            browser = self.browser_factory(config)
            browser.deadline = deadline
            navigation_start = time.monotonic()
            try:
                browser.open(url)
                trace["browser"] = getattr(browser, "metadata", {})
                trace["navigation"] = {
                    "success": True,
                    "latency": time.monotonic() - navigation_start,
                }
                metrics.navigation_latency = trace["navigation"]["latency"]
                persist()
            except Exception as exc:
                trace["navigation"] = {
                    "success": False,
                    "error": str(exc),
                    "latency": time.monotonic() - navigation_start,
                }
                metrics.navigation_latency = trace["navigation"]["latency"]
                if browser.blocked_reason:
                    status, reason = "blocked", browser.blocked_reason
                elif remaining() <= 0:
                    status, reason = "stopped", "max_seconds"
                else:
                    status, reason = "error", f"navigation_failed: {exc}"
                persist()
            else:
                snapshot, _ = observe()
                failures = waits = repeats = 0
                repeated_action = None
                while True:
                    if not getattr(browser, "alive", True):
                        status, reason = "error", "browser_closed"
                        break
                    block = snapshot.blocked_reason or getattr(browser, "blocked_reason", None)
                    if block:
                        status, reason = "blocked", str(block)
                        break
                    if remaining() <= 0:
                        status, reason = "stopped", "max_seconds"
                        break
                    if metrics.steps >= config.max_steps:
                        status, reason = "stopped", "max_steps"
                        break

                    metrics.steps += 1
                    step = {
                        "number": metrics.steps,
                        "input": None,
                        "target_count": 0,
                        "omitted": 0,
                        "input_chars": 0,
                        "usage": None,
                        "llm_latency": 0.0,
                        "action": None,
                        "reason": None,
                        "target": None,
                        "result": None,
                        "error": None,
                        "action_latency": 0.0,
                        "observe_latency": 0.0,
                        "wait_latency": 0.0,
                    }
                    trace["steps"].append(step)
                    current = DecisionInput(
                        task=task,
                        snapshot=snapshot,
                        action_space=build_action_space(snapshot),
                        history=_short_history(trace["steps"][:-1]),
                        timeout=min(config.model_timeout, remaining()),
                    )
                    current = prepare_input(
                        current, output_mode=getattr(self.model, "output_mode", "text")
                    )
                    step["input"] = {
                        "task": current.task,
                        "snapshot": _dump(current.snapshot),
                        "action_space": _dump(current.action_space),
                        "history": current.history,
                    }
                    step["target_count"] = len(current.snapshot.targets)
                    step["omitted"] = current.snapshot.omitted
                    step["input_chars"] = len(
                        compact_json(
                            request_body(current, getattr(self.model, "output_mode", "text"))
                        )
                    )

                    model_start = time.monotonic()
                    metrics.model_calls += 1
                    try:
                        reply = self.model.decide(current)
                    except ModelError as exc:
                        step["llm_latency"] = time.monotonic() - model_start
                        metrics.llm_latency += step["llm_latency"]
                        metrics.api_calls += exc.api_calls
                        metrics.record_usage(exc.input_tokens, exc.output_tokens)
                        step["usage"] = {
                            "input_tokens": exc.input_tokens,
                            "output_tokens": exc.output_tokens,
                            "api_calls": exc.api_calls,
                        }
                        metrics.failures += 1
                        metrics.model_failures += 1
                        step["error"] = str(exc)
                        failures += 1
                        persist()
                        if str(exc) == "model_refusal":
                            status, reason = "blocked", "model_refusal"
                            break
                        if exc.fatal:
                            status, reason = "error", str(exc)
                            break
                        if failures >= config.max_failures:
                            status, reason = "stopped", "max_failures"
                            break
                        if remaining() <= 0:
                            status, reason = "stopped", "max_seconds"
                            break
                        snapshot, step["observe_latency"] = observe()
                        persist()
                        continue
                    step["llm_latency"] = time.monotonic() - model_start
                    metrics.llm_latency += step["llm_latency"]
                    if isinstance(reply, ModelReply):
                        metrics.api_calls += reply.api_calls
                        metrics.record_usage(reply.input_tokens, reply.output_tokens)
                        step["usage"] = {
                            "input_tokens": reply.input_tokens,
                            "output_tokens": reply.output_tokens,
                            "api_calls": reply.api_calls,
                        }
                    else:
                        metrics.record_usage(None, None)
                    if not getattr(browser, "alive", True):
                        status, reason = "error", "browser_closed"
                        step["error"] = reason
                        persist()
                        break
                    block = browser.check_blocked()
                    if block:
                        status, reason = "blocked", str(block)
                        step["error"] = reason
                        persist()
                        break
                    if remaining() <= 0:
                        status, reason = "stopped", "max_seconds"
                        step["error"] = reason
                        persist()
                        break
                    try:
                        decision = validate_decision(reply, current.snapshot, current.action_space)
                    except (ValueError, TypeError) as exc:
                        step["error"] = f"invalid_decision: {exc}"
                        step["result"] = {
                            "success": False,
                            "executed": False,
                            "error": "invalid_decision",
                        }
                        metrics.failures += 1
                        metrics.validation_failures += 1
                        failures += 1
                        persist()
                        if failures >= config.max_failures:
                            status, reason = "stopped", "max_failures"
                            break
                        if remaining() <= 0:
                            status, reason = "stopped", "max_seconds"
                            break
                        snapshot, step["observe_latency"] = observe()
                        persist()
                        continue

                    name = _action_name(decision)
                    step["decision"] = decision.model_dump(mode="json")
                    metrics.decisions += 1
                    step["action"] = name
                    step["reason"] = decision.decision.reason
                    step["target"] = getattr(decision.decision, "target", None)
                    if not getattr(browser, "alive", True):
                        status, reason = "error", "browser_closed"
                        step["error"] = reason
                        persist()
                        break
                    block = getattr(browser, "blocked_reason", None)
                    if block:
                        status, reason = "blocked", str(block)
                        step["error"] = reason
                        persist()
                        break
                    if remaining() <= 0:
                        status, reason = "stopped", "max_seconds"
                        step["error"] = reason
                        persist()
                        break
                    if name == "DONE":
                        persist()
                        if verifier is None:
                            status, reason = "done_unverified", "done"
                        else:
                            verification = verifier(browser.page)
                            if verification not in {"passed", "failed", "unknown"}:
                                raise ValueError("verifier must return passed, failed, or unknown")
                            if remaining() <= 0:
                                status, reason = "stopped", "max_seconds"
                                break
                            status = {
                                "passed": "verified_success",
                                "failed": "verification_failed",
                                "unknown": "done_unverified",
                            }[verification]
                            reason = "done"
                        break
                    if name == "BLOCKED":
                        status, reason = "blocked", decision.decision.reason
                        persist()
                        break

                    if name == "WAIT":
                        waits += 1
                    else:
                        waits = 0

                    before_key = progress_key(snapshot)
                    key = action_key(decision, current.snapshot)
                    action_start = time.monotonic()
                    try:
                        result = browser.execute(decision)
                        step["result"] = _dump(result)
                        if result.executed:
                            metrics.browser_actions += 1
                        if result.success:
                            failures = 0
                            if result.executed:
                                metrics.successes += 1
                        else:
                            failures += 1
                            metrics.failures += 1
                            metrics.action_failures += 1
                            step["error"] = result.error or "action_failed"
                    except Exception as exc:
                        failures += 1
                        metrics.failures += 1
                        metrics.action_failures += 1
                        step["error"] = f"action_failed: {exc}"
                        step["result"] = {
                            "success": False,
                            "error": str(exc),
                            "executed": False,
                            "side_effect_possible": True,
                        }
                    step["action_latency"] = time.monotonic() - action_start
                    metrics.action_latency += step["action_latency"]
                    # An action may have changed the page even when it failed. Save that fact first.
                    persist()
                    if not getattr(browser, "alive", True):
                        status, reason = "error", "browser_closed"
                        break
                    if remaining() <= 0:
                        status, reason = "stopped", "max_seconds"
                        break
                    if failures >= config.max_failures:
                        status, reason = "stopped", "max_failures"
                        break
                    settle_start = time.monotonic()
                    try:
                        browser.settle()
                    except Exception as exc:
                        step["error"] = f"settle_failed: {exc}"
                        metrics.failures += 1
                        metrics.action_failures += 1
                        persist()
                        if not getattr(browser, "alive", True):
                            status, reason = "error", "browser_closed"
                            break
                    step["wait_latency"] = time.monotonic() - settle_start
                    metrics.wait_latency += step["wait_latency"]
                    if remaining() <= 0:
                        status, reason = "stopped", "max_seconds"
                        break
                    snapshot, step["observe_latency"] = observe()
                    persist()
                    block = snapshot.blocked_reason or getattr(browser, "blocked_reason", None)
                    if block:
                        status, reason = "blocked", str(block)
                        break
                    if waits >= config.max_waits:
                        status, reason = "stopped", "max_waits"
                        break
                    if progress_key(snapshot) == before_key:
                        repeats = repeats + 1 if repeated_action == key else 1
                        repeated_action = key
                    else:
                        repeats = 0
                        repeated_action = None
                    if repeats >= config.max_repeats:
                        status, reason = "stopped", "max_repeats"
                        break
        except KeyboardInterrupt:
            status, reason = "stopped", "interrupted"
        except Exception as exc:
            status, reason = "error", str(exc)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:
                    if status not in {"error", "stopped"}:
                        status, reason = "error", f"browser_close_failed: {exc}"
            trace["status"] = status
            trace["reason"] = reason
            trace["verification"] = verification
            persist()
        return RunResult(
            status=status,
            reason=reason,
            verification=verification,
            metrics=metrics.as_dict(time.monotonic() - started),
            trace_path=str(trace_path),
        )
