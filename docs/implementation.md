# DOMPilot 实施说明

项目按骨架与演示页面、Snapshot 与人工操作、Agent Loop、动态动作约束、
停止与恢复、Trace 与 benchmark、文档与示例七个阶段构建。
核心代码的 500～1000 行是软目标，可读性和执行正确性优先。

## 模块契约

| 接口 | 约定 |
|---|---|
| `BrowserSession.observe() -> Snapshot` | 批量提取、裁剪、生成本轮编号；节点引用只留在浏览器模块 |
| `build_action_space(snapshot) -> ActionSpace` | 完全由当前裁剪后的目标派生合法动作、编号和选项 |
| `ModelClient.decide(input) -> ModelReply` | 每轮一次请求；真实客户端与 FakeModel 共用接口 |
| `validate_decision(reply, snapshot, space)` | 严格 JSON 和类型校验，再验证 Snapshot、目标及动作兼容性 |
| `BrowserSession.execute(decision) -> ActionResult` | 独立重验合法性、原节点和语义；每份快照只消费一次执行尝试 |
| `Agent.run(url, task, verifier=None) -> RunResult` | 顺序执行、有界恢复、停止、可选可信验证与逐步 Trace |

数据模型集中在 `actions.py`。`snapshot.js` 保留原节点引用和未裁剪语义指纹，
模型只接收有限的序列化描述。页面导航、节点替换或关键语义变化会使旧引用失效；
执行器不通过新的 `nth(index)` 重定位，也不承诺检查和点击是原子事务。

动作超时可能已经产生副作用，因此先记录执行事实，再重新观察；不会自动重放。
模型的 DONE 仅表示自报完成，只有调用方提供的独立 Verifier 才能报告验证成功。

## 验证与复现

```console
uv sync --locked --python 3.12
uv run playwright install chromium
uv run ruff check .
uv run ruff format --check .
uv run python -m benchmarks.run --mode fake --repeats 5
uv build
```

离线 benchmark 包含四项任务，各重复五次；只访问本地 HTTP 演示页面，
使用独立 DOM 检查验证结果，不需要 API Key。

本地已验证 Windows 上的隔离 Chrome Context；真实 LLM、第三方网站和性能
对比不属于离线验收结论。发布的 Trace 示例已替换日期、端口、标识和机器版本。

## 规模与设计边界

核心 Python 与 DOM 提取脚本约 1700 行（含空行，不计演示页面、benchmark
及文档）。超出软目标主要用于严格数据契约、引用有效性检查、停止与错误分支、
可解释 Trace 和人工 CLI。保留扁平模块，没有引入 Agent 框架或插件抽象。

总时间限制是协作式预算：支持 timeout 的 Playwright/SDK 调用受剩余时间限制，
其他阶段返回后检查。同步 DOM 求值、卡死网页和自定义 Verifier 无法强制中断；
MVP 不提供进程看门狗。Verifier 超预算返回后保留验证真值，但不会报告运行成功。

## 发布隐私

仓库仅包含源码、虚构演示页面、锁文件、文档和脱敏 Trace 示例。`.env.example`
只含配置占位符；`.env`、实际 `runs/`、缓存、虚拟环境、浏览器下载和本地工具
均被忽略。真实任务及网页数据可能包含敏感内容，
分享本地 Trace 前必须另行检查；程序不把任意网页内容视为已经脱敏的数据。
