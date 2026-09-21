import json
from types import SimpleNamespace

import pytest

from dompilot.actions import DecisionInput, ModelReply, Snapshot, Target, build_action_space
from dompilot.model import ModelError, OpenAIModel, prepare_input


def model_input():
    snap = Snapshot(
        snapshot_id="s1",
        url="http://localhost",
        targets=[Target(id=1, kind="button", name="Search", actions=["CLICK"])],
    )
    return DecisionInput(task="Search", snapshot=snap, action_space=build_action_space(snap))


class Transport:
    def __init__(
        self,
        content='{"snapshot_id":"s1","decision":{"action":"DONE","reason":"x"}}',
        usage=None,
        refusal=None,
    ):
        self.calls = []
        self.content, self.usage, self.refusal = content, usage, refusal
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self.content, refusal=self.refusal),
                )
            ],
            usage=self.usage,
        )


def test_text_mode_needs_no_provider_schema_and_preserves_unknown_usage():
    client = Transport()
    model = OpenAIModel(
        base_url="http://localhost/v1", api_key="secret", model="test", client=client
    )
    reply = model.decide(model_input())
    assert isinstance(reply, ModelReply) and reply.api_calls == 1
    assert reply.input_tokens is None and reply.output_tokens is None
    request = client.calls[0]
    assert "response_format" not in request
    assert request["model"] == "test" and request["timeout"] <= 30
    assert "secret" not in json.dumps(model.metadata)
    assert len(request["messages"]) == 2


def test_strict_mode_sends_current_targets():
    client = Transport(usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10))
    model = OpenAIModel(
        base_url="http://localhost/v1",
        api_key="secret",
        model="test",
        output_mode="json_schema",
        client=client,
    )
    reply = model.decide(model_input())
    assert (reply.input_tokens, reply.output_tokens) == (20, 10)
    schema = client.calls[0]["response_format"]["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["decision"]["anyOf"][0]["properties"]["target"]["enum"] == [1]


def test_refusal_is_not_an_action():
    model = OpenAIModel(
        base_url="http://localhost/v1", api_key="x", model="test", client=Transport(refusal="no")
    )
    with pytest.raises(ModelError, match="model_refusal"):
        model.decide(model_input())


def test_refusal_preserves_usage_that_provider_already_returned():
    model = OpenAIModel(
        base_url="http://localhost/v1",
        api_key="x",
        model="test",
        client=Transport(
            refusal="no", usage=SimpleNamespace(prompt_tokens=50, completion_tokens=4)
        ),
    )
    with pytest.raises(ModelError) as raised:
        model.decide(model_input())
    assert (raised.value.input_tokens, raised.value.output_tokens) == (50, 4)


def test_request_budget_fits_schema_and_does_not_mutate_snapshot():
    inp = model_input()
    inp.snapshot.targets = [
        Target(id=i, kind="button", name="x" * 120, context="y" * 200, actions=["CLICK"])
        for i in range(1, 51)
    ]
    inp.history = [{"error": "x" * 5000} for _ in range(10)]
    fitted = prepare_input(inp, "json_schema")
    assert len(fitted.history) <= 5
    client = Transport()
    model = OpenAIModel(
        base_url="http://localhost/v1",
        api_key="x",
        model="test",
        output_mode="json_schema",
        client=client,
    )
    model.decide(fitted)
    request = client.calls[0]
    assert (
        len(
            json.dumps(
                {"messages": request["messages"], "response_format": request["response_format"]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        <= 32000
    )
    assert len(inp.snapshot.targets) == 50


def test_task_over_limit_rejected_before_request():
    inp = model_input()
    inp.task = "x" * 2001
    with pytest.raises(ValueError):
        prepare_input(inp)


@pytest.mark.parametrize("status", [200, 400, 429])
def test_real_sdk_local_http_compatibility_and_no_hidden_retries(status):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if status == 200:
                body = {
                    "id": "local",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "local-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "{}"},
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
                }
            else:
                body = {"error": {"message": "echoed-secret-do-not-log", "type": "test_error"}}
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model = OpenAIModel(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key="test-only-key",
        model="local-model",
        output_mode="json_schema",
    )
    try:
        if status == 200:
            reply = model.decide(model_input())
            assert (reply.input_tokens, reply.output_tokens, reply.api_calls) == (12, 2, 1)
        else:
            with pytest.raises(ModelError) as raised:
                model.decide(model_input())
            assert str(raised.value) == f"api_http_{status}"
            assert raised.value.fatal is (status == 400)
            assert raised.value.api_calls == 1
        assert len(requests) == 1
        assert requests[0]["response_format"]["json_schema"]["strict"] is True
    finally:
        model.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
