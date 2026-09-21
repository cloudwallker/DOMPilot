"""Small, explicit counters for one agent run."""

from dataclasses import dataclass


@dataclass
class RunMetrics:
    steps: int = 0
    model_calls: int = 0
    decisions: int = 0
    successes: int = 0
    failures: int = 0
    model_failures: int = 0
    validation_failures: int = 0
    action_failures: int = 0
    browser_actions: int = 0
    api_calls: int = 0
    llm_latency: float = 0.0
    action_latency: float = 0.0
    observe_latency: float = 0.0
    wait_latency: float = 0.0
    navigation_latency: float = 0.0
    _input_tokens: int = 0
    _output_tokens: int = 0
    _usage_complete: bool = True
    _replies: int = 0

    def record_usage(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self._replies += 1
        if input_tokens is None or output_tokens is None:
            self._usage_complete = False
        if input_tokens is not None:
            self._input_tokens += input_tokens
        if output_tokens is not None:
            self._output_tokens += output_tokens

    def as_dict(self, elapsed_seconds: float) -> dict:
        complete = self._usage_complete and self._replies > 0
        return {
            "steps": self.steps,
            "model_calls": self.model_calls,
            "decisions": self.decisions,
            "successes": self.successes,
            "failures": self.failures,
            "model_failures": self.model_failures,
            "validation_failures": self.validation_failures,
            "action_failures": self.action_failures,
            "browser_actions": self.browser_actions,
            "api_calls": self.api_calls,
            "input_tokens": self._input_tokens if complete else None,
            "output_tokens": self._output_tokens if complete else None,
            "total_tokens": self._input_tokens + self._output_tokens if complete else None,
            "input_tokens_known": self._input_tokens,
            "output_tokens_known": self._output_tokens,
            "total_tokens_known": self._input_tokens + self._output_tokens,
            "usage_complete": complete,
            "llm_latency": round(self.llm_latency, 6),
            "action_latency": round(self.action_latency, 6),
            "observe_latency": round(self.observe_latency, 6),
            "wait_latency": round(self.wait_latency, 6),
            "browser_latency": round(
                self.action_latency + self.observe_latency + self.wait_latency, 6
            ),
            "navigation_latency": round(self.navigation_latency, 6),
            "elapsed_seconds": round(elapsed_seconds, 6),
        }
