import pytest

from dompilot.actions import Snapshot, Target
from dompilot.cli import main, parse_manual


def test_manual_text_is_data_and_unknown_commands_rejected():
    snap = Snapshot(
        snapshot_id="s1",
        url="http://localhost",
        targets=[Target(id=1, kind="textbox", name="Search", actions=["TYPE_TEXT"])],
    )
    decision = parse_manual('TYPE_TEXT 1 "hello $(world)"', snap)
    assert decision.decision.text == "hello $(world)"
    with pytest.raises(ValueError):
        parse_manual("CLICK 1", snap)
    with pytest.raises(ValueError):
        parse_manual("JAVASCRIPT alert(1)", snap)
    with pytest.raises(ValueError):
        parse_manual("WAIT 1", snap)


def test_manual_can_clear_field():
    snap = Snapshot(
        snapshot_id="s1",
        url="http://localhost",
        targets=[Target(id=1, kind="textbox", actions=["TYPE_TEXT"])],
    )
    assert parse_manual('TYPE_TEXT 1 ""', snap).decision.text == ""


def test_help_needs_no_api_key(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "manual" in capsys.readouterr().out


def test_invalid_config_does_not_start_browser(monkeypatch, capsys):
    monkeypatch.delenv("DOMPILOT_API_KEY", raising=False)
    monkeypatch.delenv("DOMPILOT_MODEL", raising=False)
    assert main(["run", "--url", "http://localhost", "--task", "Search"]) == 2
    assert "required" in capsys.readouterr().err


def test_manual_cli_completes_local_search(site):
    import os
    import subprocess
    import sys

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    process = subprocess.run(
        [sys.executable, "-m", "dompilot", "manual", "--url", site + "search.html", "--headless"],
        input='TYPE_TEXT 1 "Transformer"\nCLICK 2\nDONE\n',
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=45,
        env=env,
    )
    assert process.returncode == 0, process.stderr
    assert "Search results: Transformer" in process.stdout
