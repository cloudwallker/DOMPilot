"""One OpenAI-compatible request per decision; execution is never exposed here."""

import os
import time
from typing import Protocol
from urllib.parse import urlsplit

from openai import APIError, APIStatusError, OpenAI

from .actions import (
    DecisionInput,
    ModelReply,
    build_action_space,
    compact_json,
    decision_schema,
    fit_snapshot,
)
from .prompts import SYSTEM_PROMPT


class ModelError(Exception):
    def __init__(
        self,
        message: str,
        fatal: bool = False,
        api_calls: int = 1,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ):
        super().__init__(message)
        self.fatal = fatal
        self.api_calls = api_calls
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ModelClient(Protocol):
    def decide(self, input: DecisionInput) -> ModelReply: ...


def request_body(input: DecisionInput, output_mode: str) -> dict:
    payload = input.model_dump(exclude={"timeout"})
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": compact_json(payload)},
        ]
    }
    if output_mode == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "dompilot_decision",
                "strict": True,
                "schema": decision_schema(input.snapshot, input.action_space),
            },
        }
    return body


def prepare_input(input: DecisionInput, output_mode: str = "text") -> DecisionInput:
    if not input.task.strip() or len(input.task) > 2000:
        raise ValueError("task_must_have_1_to_2000_characters")
    if output_mode not in {"text", "json_schema"}:
        raise ValueError("output_mode_must_be_text_or_json_schema")
    current = input.model_copy(deep=True)
    current.snapshot = fit_snapshot(current.snapshot)
    current.history = [
        {
            str(k)[:40]: v[:200] if isinstance(v, str) else v
            for k, v in entry.items()
            if k in {"action", "success", "error", "reason"}
        }
        for entry in current.history[-5:]
    ]
    while True:
        current.action_space = build_action_space(current.snapshot)
        if len(compact_json(request_body(current, output_mode))) <= 32000:
            return current
        if current.snapshot.targets:
            target = current.snapshot.targets.pop()
            current.snapshot.omitted += 1
            current.snapshot.omitted_options += len(target.options)
        elif current.history:
            current.history.pop(0)
        else:
            raise ValueError("model_input_over_budget")


class OpenAIModel:
    def __init__(
        self, *, base_url: str, api_key: str, model: str, output_mode: str = "text", client=None
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_model_base_url")
        if not api_key or not model:
            raise ValueError("DOMPILOT_API_KEY_and_DOMPILOT_MODEL_are_required")
        if output_mode not in {"text", "json_schema"}:
            raise ValueError("output_mode_must_be_text_or_json_schema")
        self.model = model
        self.output_mode = output_mode
        self.metadata = {"provider": parsed.hostname, "model": model, "output_mode": output_mode}
        self.client = client or OpenAI(
            base_url=base_url, api_key=api_key, max_retries=0, timeout=30
        )

    @classmethod
    def from_env(cls):
        return cls(
            base_url=os.environ.get("DOMPILOT_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("DOMPILOT_API_KEY", ""),
            model=os.environ.get("DOMPILOT_MODEL", ""),
            output_mode=os.environ.get("DOMPILOT_OUTPUT_MODE", "text"),
        )

    def decide(self, input: DecisionInput) -> ModelReply:
        input = prepare_input(input, self.output_mode)
        if input.timeout <= 0:
            raise ModelError("model_deadline_exceeded", fatal=True, api_calls=0)
        started = time.perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                timeout=min(30, input.timeout),
                **request_body(input, self.output_mode),
            )
        except APIStatusError as exc:
            # Do not persist provider bodies/headers: they can echo credentials or input.
            fatal = 400 <= exc.status_code < 500 and exc.status_code not in {408, 429}
            raise ModelError(f"api_http_{exc.status_code}", fatal=fatal) from None
        except APIError:
            raise ModelError("api_transport_error") from None
        usage = response.usage
        tokens = {
            "input_tokens": usage.prompt_tokens if usage else None,
            "output_tokens": usage.completion_tokens if usage else None,
        }
        if not response.choices:
            raise ModelError("empty_model_response", **tokens)
        choice = response.choices[0]
        message = choice.message
        if getattr(message, "refusal", None):
            raise ModelError("model_refusal", fatal=True, **tokens)
        if choice.finish_reason in {"length", "content_filter"}:
            raise ModelError("incomplete_model_response", **tokens)
        if not isinstance(message.content, str):
            raise ModelError("empty_model_response", **tokens)
        return ModelReply(
            text=message.content,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency=time.perf_counter() - started,
            api_calls=1,
        )

    def close(self):
        if hasattr(self.client, "close"):
            self.client.close()
