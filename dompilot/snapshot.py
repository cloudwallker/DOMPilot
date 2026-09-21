"""A bounded semantic DOM view with retained references to the observed nodes."""

from pathlib import Path

from playwright.sync_api import ElementHandle, Page

from dompilot.actions import Snapshot

_SOURCE = (Path(__file__).with_name("snapshot.js")).read_text(encoding="utf-8")
_CAPTURE = f"() => {{\n{_SOURCE}\nreturn capture();\n}}"
_SIGNATURE = f"node => {{\n{_SOURCE}\nreturn signature(node);\n}}"
_BLOCKED = f"() => {{\n{_SOURCE}\nreturn challengeReason();\n}}"


def capture_snapshot(
    page: Page, snapshot_id: str
) -> tuple[Snapshot, dict[int, ElementHandle], dict[int, dict]]:
    """Extract one viewport and hold actual DOM nodes for its lifetime."""
    root = page.evaluate_handle(_CAPTURE)
    node_array = None
    handles: dict[int, ElementHandle] = {}
    try:
        data = root.evaluate("result => result.data")
        signatures = root.evaluate("result => result.signatures")
        node_array = root.get_property("nodes")
        for index, item in enumerate(data["targets"]):
            handle = node_array.get_property(str(index)).as_element()
            if handle is not None:
                handles[item["id"]] = handle
        return (
            Snapshot(snapshot_id=snapshot_id, **data),
            handles,
            {item["id"]: signatures[index] for index, item in enumerate(data["targets"])},
        )
    except Exception:
        for handle in handles.values():
            handle.dispose()
        raise
    finally:
        if node_array is not None:
            node_array.dispose()
        root.dispose()


def current_signature(handle: ElementHandle) -> dict | None:
    """Recheck the same node, including current visibility and availability."""
    return handle.evaluate(_SIGNATURE)


def detect_blocked(page: Page) -> str | None:
    """Check only visible challenge cues without replacing the current snapshot."""
    return page.evaluate(_BLOCKED)
