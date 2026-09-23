# DOMPilot

**中文简介：** 小型浏览器 Agent，将网页 DOM 压缩为结构化快照，并验证、执行受约束的动作。

**English overview:** A small browser agent that compresses the DOM into structured snapshots and validates constrained actions before execution.

## 使用 / Usage

Python 与 Playwright 项目；可使用手动模式或兼容 OpenAI 的模型接口。主程序位于 `dompilot/`。

A Python and Playwright project with manual mode and optional OpenAI-compatible model access. The application is in `dompilot/`.

```text
python -m pip install -e .
dompilot --help
```
