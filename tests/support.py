"""Local site and deterministic model used by browser and integration tests."""

import json
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator
from urllib.parse import urlparse

PAGES = Path(__file__).resolve().parent / "pages"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def fixture_server() -> Iterator[str]:
    """Serve the local HTML fixtures and yield their base URL."""
    handler = partial(_QuietHandler, directory=str(PAGES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class FakeModel:
    """Choose actions from current target semantics, without stable target IDs."""

    def __init__(self) -> None:
        self.total_input_chars = 0
        self.total_calls = 0

    def decide(self, input):
        from dompilot.actions import ModelReply

        self.total_calls += 1
        self.total_input_chars += len(
            json.dumps(
                {
                    "task": input.task,
                    "snapshot": input.snapshot.model_dump(mode="json"),
                    "action_space": input.action_space.model_dump(mode="json"),
                    "history": input.history,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        snapshot = input.snapshot
        allowed = input.action_space.targets
        targets = snapshot.targets
        page = Path(urlparse(snapshot.url).path).stem
        feedback = snapshot.feedback or ""

        def choose(action: str, *names: str):
            ids = set(allowed.get(action, []))
            for target in targets:
                if target.id in ids and any(name in target.name.casefold() for name in names):
                    return target
            return None

        def reply(action: str, *, target=None, text=None, value=None):
            decision = {"action": action, "reason": "Follow the task using the current page state"}
            if target is not None:
                decision["target"] = target.id
            if text is not None:
                decision["text"] = text
            if value is not None:
                decision["value"] = value
            payload = {"snapshot_id": snapshot.snapshot_id, "decision": decision}
            return ModelReply(text=json.dumps(payload, ensure_ascii=False), api_calls=0)

        if page in ("search", "large_text"):
            if "Search results: Transformer" in feedback:
                return reply("DONE")
            field = choose("TYPE_TEXT", "search")
            if field is not None and field.value != "Transformer":
                return reply("TYPE_TEXT", target=field, text="Transformer")
            button = choose("CLICK", "search")
            if button is not None:
                return reply("CLICK", target=button)

        elif page == "form":
            if "Submitted: Ada; accepted" in feedback:
                return reply("DONE")
            field = choose("TYPE_TEXT", "name")
            if field is not None and field.value != "Ada":
                return reply("TYPE_TEXT", target=field, text="Ada")
            accept = choose("CLICK", "accept")
            if accept is not None and not accept.checked:
                return reply("CLICK", target=accept)
            submit = choose("CLICK", "submit")
            if submit is not None:
                return reply("CLICK", target=submit)

        elif page == "dropdown":
            if "Selected: python" in feedback:
                return reply("DONE")
            language = choose("SELECT", "language")
            if language is not None and language.value != "python":
                return reply("SELECT", target=language, value="python")
            apply = choose("CLICK", "apply")
            if apply is not None:
                return reply("CLICK", target=apply)

        elif page == "dynamic":
            if "Saved: hello" in feedback:
                return reply("DONE")
            field = choose("TYPE_TEXT", "dynamic text")
            if field is None:
                reveal = choose("CLICK", "reveal")
                if reveal is not None:
                    return reply("CLICK", target=reveal)
                return reply("WAIT")
            if field.value != "hello":
                return reply("TYPE_TEXT", target=field, text="hello")
            save = choose("CLICK", "save")
            if save is not None:
                return reply("CLICK", target=save)

        return reply("BLOCKED")
