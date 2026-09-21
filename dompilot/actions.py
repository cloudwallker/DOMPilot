"""Serializable contracts and the only legal action vocabulary."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunConfig(StrictModel):
    headless: bool = False
    max_steps: int = Field(default=20, ge=1)
    max_failures: int = Field(default=3, ge=1)
    max_waits: int = Field(default=3, ge=1)
    max_repeats: int = Field(default=3, ge=1)
    max_seconds: float = Field(default=180, gt=0)
    model_timeout: float = Field(default=30, gt=0)
    navigation_timeout: float = Field(default=15, gt=0)
    action_timeout: float = Field(default=3, gt=0)
    run_dir: Path = Path("runs")


class Option(StrictModel):
    value: str
    label: str


class Target(StrictModel):
    id: int = Field(gt=0)
    kind: str
    name: str = ""
    placeholder: str = ""
    value: str = ""
    context: str = ""
    actions: list[str]
    options: list[Option] = Field(default_factory=list)
    checked: bool | None = None
    href: str = ""


class Snapshot(StrictModel):
    snapshot_id: str
    url: str
    title: str = ""
    targets: list[Target] = Field(default_factory=list)
    feedback: str = ""
    scroll_y: float = 0
    viewport_height: float = 800
    can_scroll_up: bool = False
    can_scroll_down: bool = False
    omitted: int = 0
    omitted_options: int = 0
    blocked_reason: str | None = None


class ActionSpace(StrictModel):
    targets: dict[str, list[int]] = Field(default_factory=dict)
    select_options: dict[int, list[str]] = Field(default_factory=dict)
    controls: list[str] = Field(default_factory=lambda: ["WAIT", "DONE", "BLOCKED"])


class BaseAction(StrictModel):
    reason: str = Field(max_length=200)


class Click(BaseAction):
    action: Literal["CLICK"]
    target: int = Field(gt=0)


class TypeText(BaseAction):
    action: Literal["TYPE_TEXT"]
    target: int = Field(gt=0)
    text: str = Field(max_length=2000)


class Select(BaseAction):
    action: Literal["SELECT"]
    target: int = Field(gt=0)
    value: str = Field(max_length=200)


class Control(BaseAction):
    action: Literal["SCROLL_UP", "SCROLL_DOWN", "WAIT", "DONE", "BLOCKED"]


class AgentDecision(StrictModel):
    snapshot_id: str
    decision: Annotated[Click | TypeText | Select | Control, Field(discriminator="action")]


class DecisionInput(StrictModel):
    task: str
    snapshot: Snapshot
    action_space: ActionSpace
    history: list[dict] = Field(default_factory=list)
    timeout: float = 30


class ModelReply(StrictModel):
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency: float = 0
    api_calls: int = 0


class ActionResult(StrictModel):
    success: bool
    error: str | None = None
    latency: float = 0
    side_effect_possible: bool = False
    executed: bool = False


class RunResult(StrictModel):
    status: str
    reason: str
    verification: str = "unknown"
    metrics: dict = Field(default_factory=dict)
    trace_path: str = ""


def compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_action_space(snapshot: Snapshot) -> ActionSpace:
    space = ActionSpace()
    for target in snapshot.targets:
        for action in target.actions:
            if action not in {"CLICK", "TYPE_TEXT", "SELECT"}:
                continue
            if action == "SELECT":
                if not target.options:
                    continue
                space.select_options[target.id] = [o.value for o in target.options]
            space.targets.setdefault(action, []).append(target.id)
    if snapshot.can_scroll_up:
        space.controls.append("SCROLL_UP")
    if snapshot.can_scroll_down:
        space.controls.append("SCROLL_DOWN")
    return space


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid_json_constant: {value}")


def validate_decision(
    reply: ModelReply | str, snapshot: Snapshot, action_space: ActionSpace
) -> AgentDecision:
    raw = reply.text if isinstance(reply, ModelReply) else reply
    if len(raw) > 16000:
        raise ValueError("model_output_too_large")
    obj = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    decision = AgentDecision.model_validate(obj)
    if decision.snapshot_id != snapshot.snapshot_id:
        raise ValueError("stale_snapshot")
    action = decision.decision
    if isinstance(action, Control):
        if action.action not in action_space.controls:
            raise ValueError("unavailable_action")
    else:
        if action.target not in action_space.targets.get(action.action, []):
            raise ValueError("invalid_target")
        if isinstance(action, Select) and action.value not in action_space.select_options.get(
            action.target, []
        ):
            raise ValueError("invalid_option")
    return decision


def decision_schema(snapshot: Snapshot, action_space: ActionSpace) -> dict:
    """Build a provider-compatible schema from the exact exposed action space."""
    branches = []

    def branch(action, properties):
        fields = {
            "action": {"type": "string", "enum": [action]},
            **properties,
            "reason": {"type": "string", "maxLength": 200},
        }
        return {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        }

    for action in ("CLICK", "TYPE_TEXT", "SELECT"):
        ids = action_space.targets.get(action, [])
        if not ids:
            continue
        if action == "SELECT":
            for target in ids:
                branches.append(
                    branch(
                        action,
                        {
                            "target": {"type": "integer", "enum": [target]},
                            "value": {
                                "type": "string",
                                "enum": action_space.select_options[target],
                            },
                        },
                    )
                )
        else:
            props = {"target": {"type": "integer", "enum": ids}}
            if action == "TYPE_TEXT":
                props["text"] = {"type": "string", "maxLength": 2000}
            branches.append(branch(action, props))
    branches.extend(branch(action, {}) for action in action_space.controls)
    return {
        "type": "object",
        "properties": {
            "snapshot_id": {"type": "string", "enum": [snapshot.snapshot_id]},
            "decision": {"anyOf": branches},
        },
        "required": ["snapshot_id", "decision"],
        "additionalProperties": False,
    }


def fit_snapshot(snapshot: Snapshot, limit: int = 12000) -> Snapshot:
    snapshot = snapshot.model_copy(deep=True)
    while (
        len(
            compact_json(
                {
                    "snapshot": snapshot.model_dump(),
                    "action_space": build_action_space(snapshot).model_dump(),
                }
            )
        )
        > limit
    ):
        if snapshot.targets:
            removed = snapshot.targets.pop()
            snapshot.omitted += 1
            snapshot.omitted_options += len(removed.options)
        elif snapshot.feedback:
            snapshot.feedback = snapshot.feedback[: len(snapshot.feedback) // 2]
        else:
            raise ValueError("snapshot_metadata_over_budget")
    return snapshot


def progress_key(snapshot: Snapshot) -> str:
    targets = [t.model_dump(exclude={"id", "context"}) for t in snapshot.targets]
    return compact_json(
        [snapshot.url, snapshot.title, snapshot.scroll_y, snapshot.feedback, targets]
    )


def action_key(decision: AgentDecision, snapshot: Snapshot) -> str:
    action = decision.decision.model_dump(exclude={"reason", "target"})
    target_id = getattr(decision.decision, "target", None)
    target = next((t for t in snapshot.targets if t.id == target_id), None)
    if target:
        action["target_identity"] = [target.kind, target.name, target.placeholder, target.href]
    return compact_json(action)
