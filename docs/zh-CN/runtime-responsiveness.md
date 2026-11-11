# 运行耗时分析与排查

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../runtime-responsiveness.md)

[分析文件](#profile-files) · [如何读结果](#reading-the-results) · [运行时如何处理慢存储](#runtime-behavior) · [终端输出失效时](#terminal-output-failures) · [可复现的测量](#repeatable-measurements)

Astra 可选记录阶段耗时，帮助区分模型等待、工具执行、存储和界面更新。设置 `ASTRA_PROFILE_QUERY=1` 后重启 Astra，复现问题，再正常退出，让已接收的记录写完。

在安装仓库中使用该安装的 Python 运行以下命令（macOS/Linux 为 `.venv/bin/python`，Windows 为 `.venv\Scripts\python.exe`）：

```text
python -m agent.runtime.latency
python -m agent.runtime.query_profiler
python scripts/benchmark_runtime_responsiveness.py --samples 20 --lock-ms 150
python scripts/benchmark_runtime_responsiveness.py --samples 50 --lock-ms 0
```

<a id="profile-files"></a>

## 分析文件

启用后，`.astra/query-profile.jsonl` 记录模型请求阶段，`.astra/runtime-profile.jsonl` 记录工具、存储和前端阶段。设置 `ASTRA_HOME` 时，文件保存在该目录。运行时记录使用有界队列，单文件上限 5 MiB，并保留两个轮转备份。最终摘要会报告丢弃记录、写入错误和未确认样本；不完整运行标记 `complete: false`。用 `--generation ID` 可查看更早的后端运行周期。

后端空闲时按定时器做的事（每秒查看一次其他会话的来信、每五秒报一次在线）只在耗时达到 50 毫秒时记录。每次都记的话，这些轮询几小时内就会写满文件，把真正要留的请求记录挤掉。

分析默认关闭，不改变模型选择或推理设置。

<a id="reading-the-results"></a>

## 如何读结果

| 阶段 | 可以解释什么 |
| --- | --- |
| 请求准备、首个模型事件 | 本地准备与等待服务方的时间 |
| 首段推理、首段回答 | 不同输出通道何时开始可用 |
| 工具排队、审批、执行 | 等待准入与工具内部工作的时间 |
| 结果处理、持久化 | 序列化、存储和交付开销 |
| 前端处理、React commit | 事件到达后的界面处理时间 |

报告默认汇总最近一个后端运行周期，包含样本数、中位数、p95、最大值和丢弃数量。各进程使用自己的单调时钟；React commit 不能当作终端像素实际显示时间。记录规模受限，标识经过清理，不需要保存对话正文或原始工具输出。

<a id="runtime-behavior"></a>

## 运行时如何处理慢存储

任务和审批写入、会话保存、Session Recall 持久化都在事件循环之外运行。有界、有序的事件写入器保持交付顺序并施加背压。工具仍须在派发前取得持久化执行声明。取消时会排空已接收的写入，并保留未知结果，避免因记录不完整而自动重放动作。

这样可以让慢存储不阻塞其他控制操作，但不会让被锁住的数据库更快写完，也不会减少模型的推理预算。正常退出会等待已接收写入及 worker 清理完成。

<a id="terminal-output-failures"></a>

## 终端输出失效时

动态预览按菜单、输入框和面板实际占用的空间收缩，长输入只改变显示视口，提交原文和已保存历史保持完整。Apple Terminal 的连续文本预览按 80 ms 合并，控制事件仍按原顺序立即处理。

终端写入交给 Astra 自己启动的小进程，避免坏掉的终端阻塞 TUI 的输入和退出。积压上限为 4 MiB；有数据待写且连续五秒没有写入进展时，停止绘制并请求后端取消任务、保存会话。正常等待模型或用户不会触发此超时。

后端有十秒完成退出，之后才尝试终止；事件写线程最多等待两秒排空。任务记录为中断，重新打开会话不会自动重放工具。若被迫终止，正在执行的外部操作及其最新结果仍可能不完整，需要按恢复后的中断记录核对。

`.logs/tui-terminal.jsonl` 仅记录字节数、最高积压、写入耗时、清屏请求、故障码和退出是否被迫终止，不记录对话内容或终端原始输出。更新后重启 Astra 生效。这些措施降低渲染压力并隔离输出故障，尚不能认定解决了 Terminal.app 或输入法本身的崩溃。

<a id="repeatable-measurements"></a>

## 可复现的测量

基准使用临时 SQLite 数据库和受控本地工作负载，不发送模型请求。比较同一工作负载在有、无注入锁压力时的表现，保留失败和长尾样本。结果衡量的是本地调度与持久化，不能代表网络延迟或回答质量。

将单次报告连同 commit、平台和相关配置保存在 `output/` 或 `.astra/artifacts/`。进一步优化应依据新做的固定条件测量；历史通过数量和某台机器的耗时不能当作当前版本保证。
