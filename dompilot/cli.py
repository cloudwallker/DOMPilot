"""Two small entry points: inspect/act manually, or run the model loop."""

import argparse
import json
import shlex
import sys
from pathlib import Path

from .actions import RunConfig, build_action_space, compact_json, validate_decision
from .agent import Agent
from .browser import BrowserSession
from .model import OpenAIModel


def parse_manual(command, snapshot):
    words = shlex.split(command)
    if not words:
        raise ValueError("empty_command")
    name, *args = words
    name = name.upper()
    required = {
        "CLICK": 1,
        "TYPE_TEXT": 2,
        "SELECT": 2,
        "SCROLL_UP": 0,
        "SCROLL_DOWN": 0,
        "WAIT": 0,
        "DONE": 0,
        "BLOCKED": 0,
    }
    if name not in required or len(args) != required[name]:
        raise ValueError('使用 CLICK 1、TYPE_TEXT 1 "text"、SELECT 1 "value" 或无参数动作')
    action = {"action": name, "reason": "人工输入"}
    if args:
        action["target"] = int(args[0])
    if name in {"TYPE_TEXT", "SELECT"}:
        action["text" if name == "TYPE_TEXT" else "value"] = args[1]
    return validate_decision(
        compact_json({"snapshot_id": snapshot.snapshot_id, "decision": action}),
        snapshot,
        build_action_space(snapshot),
    )


def manual(url, config):
    with BrowserSession(config) as browser:
        browser.open(url)
        while True:
            snapshot = browser.observe()
            if snapshot.blocked_reason or browser.blocked_reason:
                print(f"BLOCKED: {snapshot.blocked_reason or browser.blocked_reason}")
                return 2
            print(f"\n{snapshot.snapshot_id}  {snapshot.title}\n{snapshot.url}")
            print(snapshot.feedback)
            for target in snapshot.targets:
                print(
                    f"[{target.id}] {target.kind} — {target.name} "
                    f"value={target.value!r} actions={','.join(target.actions)}"
                )
                if target.options:
                    print("    options:", compact_json([o.model_dump() for o in target.options]))
            print("controls:", ", ".join(build_action_space(snapshot).controls))
            try:
                decision = parse_manual(input("dompilot> "), snapshot)
            except EOFError:
                return 0
            except ValueError as exc:
                print(f"输入被拒绝：{exc}")
                continue
            if decision.decision.action in {"DONE", "BLOCKED"}:
                print("手动会话结束。DONE 不代表已经独立验证。")
                return 0 if decision.decision.action == "DONE" else 2
            result = browser.execute(decision)
            print(compact_json(result.model_dump()))
            browser.settle()


def main(argv=None):
    parser = argparse.ArgumentParser(description="DOMPilot：学习结构化动作空间的微型浏览器 Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("manual", "run"):
        child = commands.add_parser(name)
        child.add_argument("--url", required=True)
        child.add_argument("--headless", action="store_true")
        if name == "run":
            child.add_argument("--task", required=True)
            child.add_argument("--max-steps", type=int, default=20)
            child.add_argument("--run-dir", type=Path, default=Path("runs"))
    args = parser.parse_args(argv)
    model = None
    try:
        config = RunConfig(
            headless=args.headless,
            max_steps=getattr(args, "max_steps", 20),
            run_dir=getattr(args, "run_dir", Path("runs")),
        )
        if args.command == "manual":
            return manual(args.url, config)
        model = OpenAIModel.from_env()
        result = Agent(model, config).run(args.url, args.task)
        if result.status == "done_unverified":
            print("模型认为任务完成，尚未独立验证。")
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        if result.reason == "interrupted":
            return 130
        return {
            "done_unverified": 0,
            "verified_success": 0,
            "blocked": 2,
            "stopped": 3,
            "verification_failed": 1,
            "error": 1,
        }[result.status]
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        print(f"配置或输入错误：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if model is not None:
            model.close()


if __name__ == "__main__":
    raise SystemExit(main())
