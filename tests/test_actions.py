import json

import jsonschema
import pytest

from dompilot.actions import (
    ModelReply,
    Option,
    Snapshot,
    Target,
    action_key,
    build_action_space,
    compact_json,
    decision_schema,
    fit_snapshot,
    progress_key,
    validate_decision,
)


@pytest.fixture
def snapshot():
    return Snapshot(
        snapshot_id="s1",
        url="http://localhost/",
        targets=[
            Target(id=1, kind="textbox", name="Search", actions=["TYPE_TEXT"]),
            Target(id=2, kind="button", name="Search", actions=["CLICK"]),
            Target(
                id=3,
                kind="select",
                name="Language",
                actions=["SELECT"],
                options=[Option(value="py", label="Python")],
            ),
            Target(
                id=4,
                kind="select",
                name="Colour",
                actions=["SELECT"],
                options=[Option(value="red", label="Red")],
            ),
        ],
    )


def payload(action="CLICK", **kwargs):
    return {"snapshot_id": "s1", "decision": {"action": action, "reason": "test", **kwargs}}


def test_dynamic_schema_and_validation_agree(snapshot):
    space = build_action_space(snapshot)
    schema = decision_schema(snapshot, space)
    jsonschema.Draft202012Validator.check_schema(schema)
    for obj in [
        payload(target=2),
        payload("TYPE_TEXT", target=1, text=""),
        payload("SELECT", target=3, value="py"),
        payload("DONE"),
        payload("WAIT"),
    ]:
        jsonschema.validate(obj, schema)
        assert validate_decision(json.dumps(obj), snapshot, space).snapshot_id == "s1"
    assert "SCROLL_DOWN" not in space.controls


@pytest.mark.parametrize(
    "obj",
    [
        payload(target=1),
        payload(target=999),
        payload(target=True),
        payload(target="2"),
        payload(target=-1),
        payload(target=2, selector="#x"),
        payload("WAIT", target=2),
        payload("TYPE_TEXT", target=1),
        payload("SELECT", target=3, value="red"),
        payload("SELECT", target=4, value="py"),
        payload("SCROLL_DOWN"),
        {"snapshot_id": "old", "decision": {"action": "CLICK", "target": 2, "reason": "x"}},
    ],
)
def test_illegal_combinations_never_pass(snapshot, obj):
    space = build_action_space(snapshot)
    with pytest.raises(ValueError):
        validate_decision(json.dumps(obj), snapshot, space)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, decision_schema(snapshot, space))


@pytest.mark.parametrize(
    "text",
    [
        "bad",
        "```json\n{}\n```",
        "{} {}",
        '{"snapshot_id":"s1","snapshot_id":"old"}',
        '{"snapshot_id":"s1","decision":{"action":"WAIT","reason":NaN}}',
    ],
)
def test_malformed_json_is_not_repaired(snapshot, text):
    with pytest.raises(ValueError):
        validate_decision(ModelReply(text=text), snapshot, build_action_space(snapshot))


def test_budget_preserves_whole_records_and_space(snapshot):
    huge = snapshot.model_copy(
        update={
            "targets": [
                Target(id=i, kind="button", name="名" * 120, context="x" * 200, actions=["CLICK"])
                for i in range(1, 51)
            ]
        }
    )
    fitted = fit_snapshot(huge)
    assert (
        len(
            compact_json(
                {
                    "snapshot": fitted.model_dump(),
                    "action_space": build_action_space(fitted).model_dump(),
                }
            )
        )
        <= 12000
    )
    assert 0 < len(fitted.targets) < 50
    assert fitted.omitted == 50 - len(fitted.targets)
    assert build_action_space(fitted).targets["CLICK"] == [t.id for t in fitted.targets]


def test_repeat_identity_does_not_depend_on_temporary_number(snapshot):
    changed = snapshot.model_copy(deep=True)
    changed.snapshot_id = "s2"
    changed.targets[1].id = 22
    one = validate_decision(json.dumps(payload(target=2)), snapshot, build_action_space(snapshot))
    two = validate_decision(
        json.dumps(
            {"snapshot_id": "s2", "decision": {"action": "CLICK", "target": 22, "reason": "again"}}
        ),
        changed,
        build_action_space(changed),
    )
    assert action_key(one, snapshot) == action_key(two, changed)
    assert progress_key(snapshot) == progress_key(changed)
    changed.scroll_y = 100
    assert progress_key(snapshot) != progress_key(changed)
