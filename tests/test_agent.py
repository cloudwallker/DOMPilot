"""Bounded serial agent loop and durable trace contract."""

import json
import time
from pathlib import Path

import pytest

from dompilot.actions import ActionResult, ModelReply, RunConfig, Snapshot, Target
from dompilot.agent import Agent
from dompilot.model import ModelError


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def snapshot(number=1, *, target_id=1, feedback="", blocked_reason=None):
    return Snapshot(
        snapshot_id=f"s{number}",
        url="http://localhost/test",
        feedback=feedback,
        blocked_reason=blocked_reason,
        targets=[Target(id=target_id, kind="button", name="Submit", actions=["CLICK"])],
    )


def reply(current, action="DONE", **fields):
    return ModelReply(
        text=json.dumps(
            {
                "snapshot_id": current.snapshot_id,
                "decision": {"action": action, "reason": "test", **fields},
            }
        )
    )


class FakeBrowser:
    def __init__(self, snapshots, *, results=None):
        self.snapshots = iter(snapshots)
        self.results = iter(results or [])
        self.events = []
        self.page = object()
        self.alive = True
        self.blocked_reason = None
        self.deadline = None

    def open(self, url):
        self.events.append("open")

    def observe(self):
        self.events.append("observe")
        return next(self.snapshots)

    def check_blocked(self):
        return self.blocked_reason

    def execute(self, decision):
        self.events.append("execute")
        return next(self.results, ActionResult(success=True, executed=True))

    def settle(self):
        self.events.append("settle")
        return 0.01

    def close(self):
        self.events.append("close")
        self.alive = False


class FakeModel:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.inputs = []

    def decide(self, current):
        self.inputs.append(current)
        choice = next(self.choices)
        if isinstance(choice, BaseException):
            raise choice
        if callable(choice):
            return choice(current)
        return choice


def make_agent(browser, model, tmp_path, **config):
    return Agent(model, RunConfig(run_dir=tmp_path, **config), browser_factory=lambda _: browser)


def read_trace(result):
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))


def test_done_verifies_without_browser_action_and_preserves_unknown_usage(tmp_path):
    browser = FakeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])
    result = make_agent(browser, model, tmp_path).run(
        "http://localhost/test", "finish", verifier=lambda page: "passed"
    )
    trace = read_trace(result)
    assert (result.status, result.verification) == ("verified_success", "passed")
    assert browser.events == ["open", "observe", "close"]
    assert result.metrics["browser_actions"] == 0
    assert result.metrics["api_calls"] == 0
    assert result.metrics["model_calls"] == 1
    assert result.metrics["input_tokens"] is None
    assert result.metrics["output_tokens"] is None
    assert result.metrics["total_tokens"] is None
    assert result.metrics["input_tokens_known"] == 0
    assert result.metrics["output_tokens_known"] == 0
    assert result.metrics["total_tokens_known"] == 0
    assert trace["trace_version"] == 1
    assert trace["config"]["run_dir"] == str(tmp_path)
    assert len(trace["steps"]) == 1


def test_failed_done_reports_verification_failure(tmp_path):
    browser = FakeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])
    result = make_agent(browser, model, tmp_path).run(
        "http://localhost/test", "finish", verifier=lambda page: "failed"
    )
    assert result.status == "verification_failed"


def test_action_fact_is_saved_before_settle_and_observed_again(tmp_path):
    model = FakeModel(
        [
            lambda current: reply(current.snapshot, "CLICK", target=1),
            lambda current: reply(current.snapshot),
        ]
    )

    class TraceCheckingBrowser(FakeBrowser):
        def settle(self):
            traces = list(tmp_path.glob("run_*.json"))
            assert len(traces) == 1
            step = json.loads(traces[0].read_text(encoding="utf-8"))["steps"][0]
            assert step["result"]["executed"] is True
            return super().settle()

    browser = TraceCheckingBrowser([snapshot(1), snapshot(2, feedback="saved")])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.status == "done_unverified"
    assert browser.events == ["open", "observe", "execute", "settle", "observe", "close"]
    assert result.metrics["browser_actions"] == 1
    assert result.metrics["successes"] == 1
    assert result.metrics["browser_latency"] >= 0
    assert result.metrics["navigation_latency"] >= 0


def test_invalid_decisions_count_as_steps_and_stop_after_failures(tmp_path):
    browser = FakeBrowser([snapshot(i) for i in range(1, 4)])
    model = FakeModel([ModelReply(text="not json") for _ in range(3)])
    result = make_agent(browser, model, tmp_path, max_steps=10, max_failures=3).run(
        "http://localhost/test", "submit"
    )
    assert result.status == "stopped"
    assert len(read_trace(result)["steps"]) == 3
    assert result.metrics["browser_actions"] == 0
    assert result.metrics["input_tokens"] is None
    assert result.metrics["usage_complete"] is False
    assert result.metrics["validation_failures"] == 3
    assert browser.events.count("observe") == 3


@pytest.mark.parametrize("limit", [1, 3])
def test_wait_limit_is_bounded_and_each_wait_is_traced(tmp_path, limit):
    browser = FakeBrowser([snapshot(i) for i in range(1, limit + 2)])
    model = FakeModel([lambda current: reply(current.snapshot, "WAIT") for _ in range(limit)])
    result = make_agent(browser, model, tmp_path, max_waits=limit).run(
        "http://localhost/test", "submit"
    )
    assert result.status == "stopped"
    assert result.reason == "max_waits"
    assert len(read_trace(result)["steps"]) == limit
    assert browser.events.count("execute") == limit
    assert browser.events.count("settle") == limit
    assert result.metrics["browser_actions"] == limit
    assert all(step["result"]["executed"] for step in read_trace(result)["steps"])


def test_non_wait_action_resets_wait_counter(tmp_path):
    browser = FakeBrowser([snapshot(i, feedback=str(i)) for i in range(1, 8)])

    def wait(current):
        return reply(current.snapshot, "WAIT")

    def click(current):
        return reply(current.snapshot, "CLICK", target=1)

    model = FakeModel([wait, wait, click, wait, wait, wait])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.reason == "max_waits"
    assert browser.events.count("execute") == 6


def test_semantically_same_action_stops_despite_renumbered_targets(tmp_path):
    browser = FakeBrowser([snapshot(i, target_id=i) for i in range(1, 5)])
    model = FakeModel(
        [
            lambda current: reply(current.snapshot, "CLICK", target=current.snapshot.targets[0].id)
            for _ in range(3)
        ]
    )
    result = make_agent(browser, model, tmp_path, max_repeats=3).run(
        "http://localhost/test", "submit"
    )
    assert result.status == "stopped"
    assert browser.events.count("execute") == 3
    assert len(read_trace(result)["steps"]) == 3


def test_challenge_blocks_before_model_call(tmp_path):
    browser = FakeBrowser([snapshot(blocked_reason="captcha")])
    model = FakeModel([])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.status == "blocked"
    assert model.inputs == []


def test_keyboard_interrupt_returns_stopped_and_persists_trace(tmp_path):
    browser = FakeBrowser([snapshot()])
    model = FakeModel([KeyboardInterrupt()])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert (result.status, result.reason) == ("stopped", "interrupted")
    assert browser.events[-1] == "close"
    assert read_trace(result)["trace_version"] == 1


def test_model_timeout_is_capped_by_remaining_run_budget(tmp_path, clock):
    browser = FakeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])
    result = make_agent(browser, model, tmp_path, max_seconds=1, model_timeout=30).run(
        "http://localhost/test", "short task"
    )
    assert result.status == "done_unverified"
    assert 0 < model.inputs[0].timeout <= 1


def test_long_task_is_rejected_before_browser_starts(tmp_path):
    browser = FakeBrowser([])
    model = FakeModel([])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "x" * 2001)
    assert result.status == "error"
    assert "task" in result.reason
    assert browser.events == []
    assert read_trace(result)["steps"] == []


def test_deadline_crossed_during_model_call_never_executes_action(tmp_path, clock):
    browser = FakeBrowser([snapshot()])

    def slow_click(current):
        clock[0] += 0.04
        return reply(current.snapshot, "CLICK", target=1)

    model = FakeModel([slow_click])
    result = make_agent(browser, model, tmp_path, max_seconds=0.02).run(
        "http://localhost/test", "submit"
    )
    assert result.status == "stopped"
    assert browser.events.count("execute") == 0


def test_deadline_crossed_during_action_stops_before_settle(tmp_path, clock):
    class SlowBrowser(FakeBrowser):
        def execute(self, decision):
            clock[0] += 0.04
            return super().execute(decision)

    browser = SlowBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot, "CLICK", target=1)])
    result = make_agent(browser, model, tmp_path, max_seconds=0.02).run(
        "http://localhost/test", "submit"
    )
    assert (result.status, result.reason) == ("stopped", "max_seconds")
    assert "settle" not in browser.events
    assert read_trace(result)["steps"][0]["result"]["executed"] is True


def test_browser_disconnect_after_action_is_error_and_fact_is_saved(tmp_path):
    class DisconnectingBrowser(FakeBrowser):
        def execute(self, decision):
            result = super().execute(decision)
            self.alive = False
            return result

    browser = DisconnectingBrowser(
        [snapshot()], results=[ActionResult(success=True, executed=True)]
    )
    model = FakeModel([lambda current: reply(current.snapshot, "CLICK", target=1)])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert (result.status, result.reason) == ("error", "browser_closed")
    assert read_trace(result)["steps"][0]["result"]["executed"] is True
    assert "settle" not in browser.events


def test_challenge_appearing_during_model_call_prevents_action(tmp_path):
    browser = FakeBrowser([snapshot()])

    def challenge(current):
        browser.blocked_reason = "captcha"
        return reply(current.snapshot, "CLICK", target=1)

    result = make_agent(browser, FakeModel([challenge]), tmp_path).run(
        "http://localhost/test", "submit"
    )
    assert result.status == "blocked"
    assert browser.events.count("execute") == 0


def test_recoverable_model_error_reobserves_and_counts_actual_api_calls(tmp_path):
    browser = FakeBrowser([snapshot(1), snapshot(2)])
    model = FakeModel(
        [ModelError("transport", api_calls=1), lambda current: reply(current.snapshot)]
    )
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.status == "done_unverified"
    assert browser.events.count("observe") == 2
    assert result.metrics["api_calls"] == 1
    assert result.metrics["model_failures"] == 1
    assert result.metrics["model_calls"] == 2
    assert result.metrics["input_tokens"] is None
    assert len(read_trace(result)["steps"]) == 2


@pytest.mark.parametrize(
    "error,status",
    [
        (ModelError("bad_config", fatal=True, api_calls=0), "error"),
        (ModelError("model_refusal", fatal=True, api_calls=1), "blocked"),
    ],
)
def test_fatal_model_errors_end_without_retries(tmp_path, error, status):
    browser = FakeBrowser([snapshot()])
    model = FakeModel([error])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.status == status
    assert browser.events.count("observe") == 1
    assert len(read_trace(result)["steps"]) == 1


def test_model_output_mode_is_used_for_input_preparation(tmp_path, monkeypatch):
    import dompilot.agent as agent_module

    seen = []
    real_prepare = agent_module.prepare_input

    def prepare(current, output_mode="text"):
        seen.append(output_mode)
        return real_prepare(current, output_mode)

    monkeypatch.setattr(agent_module, "prepare_input", prepare)
    browser = FakeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])
    model.output_mode = "json_schema"
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.status == "done_unverified"
    assert seen == ["json_schema"]


def test_navigation_error_has_separate_trace_and_no_decision(tmp_path):
    class FailingBrowser(FakeBrowser):
        def open(self, url):
            raise RuntimeError("navigation broke")

    browser = FailingBrowser([])
    result = make_agent(browser, FakeModel([]), tmp_path).run("http://localhost/test", "submit")
    trace = read_trace(result)
    assert result.status == "error"
    assert trace["navigation"]["success"] is False
    assert trace["steps"] == []


def test_unsupported_interaction_during_navigation_is_blocked(tmp_path):
    class PopupBrowser(FakeBrowser):
        def open(self, url):
            self.blocked_reason = "popup_blocked"
            raise RuntimeError(self.blocked_reason)

    browser = PopupBrowser([])
    result = make_agent(browser, FakeModel([]), tmp_path).run("http://localhost/test", "submit")
    assert (result.status, result.reason) == ("blocked", "popup_blocked")


def test_navigation_budget_expiry_is_a_guard_stop(tmp_path, clock):
    class SlowNavigation(FakeBrowser):
        def open(self, url):
            clock[0] += 2
            raise TimeoutError("deadline_exceeded")

    browser = SlowNavigation([])
    result = make_agent(browser, FakeModel([]), tmp_path, max_seconds=1).run(
        "http://localhost/test", "submit"
    )
    assert (result.status, result.reason) == ("stopped", "max_seconds")


def test_trace_retains_exact_text_action(tmp_path):
    first = snapshot()
    first.targets = [Target(id=1, kind="textbox", name="Name", actions=["TYPE_TEXT"])]
    browser = FakeBrowser([first, snapshot(2)])
    model = FakeModel(
        [
            lambda current: reply(current.snapshot, "TYPE_TEXT", target=1, text="Ada Lovelace"),
            lambda current: reply(current.snapshot),
        ]
    )
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "fill name")
    assert read_trace(result)["steps"][0]["decision"]["decision"]["text"] == "Ada Lovelace"


def test_known_error_usage_is_not_discarded(tmp_path):
    browser = FakeBrowser([snapshot()])
    error = ModelError("model_refusal", fatal=True)
    error.input_tokens, error.output_tokens = 50, 4
    result = make_agent(browser, FakeModel([error]), tmp_path).run("http://localhost/test", "x")
    assert result.metrics["total_tokens"] == 54
    assert read_trace(result)["steps"][0]["usage"]["input_tokens"] == 50


def test_input_characters_include_system_prompt_and_dynamic_schema(tmp_path):
    from dompilot.actions import compact_json
    from dompilot.model import request_body

    browser = FakeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])
    model.output_mode = "json_schema"
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    record = read_trace(result)["steps"][0]
    assert record["input_chars"] == len(compact_json(request_body(model.inputs[0], "json_schema")))
    assert record["input_chars"] > len(compact_json(record["input"]))


def test_live_challenge_before_done_prevents_false_completion(tmp_path):
    class ChallengeBrowser(FakeBrowser):
        def check_blocked(self):
            return "verification_challenge"

    browser = ChallengeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])
    result = make_agent(browser, model, tmp_path).run("http://localhost/test", "submit")
    assert result.status == "blocked"
    assert result.reason == "verification_challenge"


def test_verifier_crossing_deadline_preserves_truth_but_does_not_claim_run_success(tmp_path, clock):
    browser = FakeBrowser([snapshot()])
    model = FakeModel([lambda current: reply(current.snapshot)])

    def slow_verifier(_):
        clock[0] += 0.08
        return "passed"

    result = make_agent(browser, model, tmp_path, max_seconds=0.05).run(
        "http://localhost/test", "submit", slow_verifier
    )
    assert (result.status, result.reason, result.verification) == (
        "stopped",
        "max_seconds",
        "passed",
    )
