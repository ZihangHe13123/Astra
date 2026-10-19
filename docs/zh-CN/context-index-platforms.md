# macOS 与 Windows 上的 Context Index

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../context-index-platforms.md)

[macOS](#macos) · [Windows](#windows) · [数据准备与启用](#data-and-enabling)

Astra 在 macOS 上默认使用 MLX，在 Windows/Linux 上使用兼容 OpenAI 接口的 llama.cpp 嵌入服务。可用 `ASTRA_EMBEDDING_BACKEND` 显式覆盖平台检测。macOS 保留 `mlx-community/Qwen3-Embedding-4B-mxfp8` 模型和旧版默认索引 `.astra/context-vectors.db`。两端均可安装 `embedding` extra；MLX 仅在 macOS 安装，numpy 在两端均可用。

<a id="macos"></a>

## macOS

Astra 自带的 `embedding_worker.py` 持有 MLX 模型。同一运行目录、同一模型标识的本地会话共享一个 worker，新增会话不会重复加载一份 4B 权重。通信使用 Astra 代码和 Python 标准库提供的带认证回环连接，不需要额外记忆服务、常驻系统守护程序或第三方包。

已有向量文件时，启动预热或显式开启推荐可以启动 worker；显式准备索引也可以在没有向量文件时启动它。查询本身不会启动 worker，也不会等待模型加载。worker 忙碌或不可用时使用文本检索。活跃客户端每 30 秒检查一次 worker，但只有编码请求才算使用：超过 `ASTRA_EMBEDDING_IDLE_SECONDS`（默认 900 秒，最少 60 秒）没有编码请求，worker 就退出并释放模型，Astra 仍在运行时也是如此。下一个编码请求会在查询路径之外通过后台逻辑重新启动它，这次请求使用文本检索。因此长时间停顿后的第一轮没有语义检索。

私有端点和令牌文件默认位于 `~/.astra/embedding-runtime`，可用 `ASTRA_EMBEDDING_RUNTIME_DIR` 修改。使用不同目录会产生独立 worker。查看状态：

```console
python -m agent.runtime.context_index.embedding_runtime status
```

该命令显示进程 PID、就绪和忙碌状态，以及 MLX 的 active/cache/peak 内存字节数，不会启动 worker。`stop` 通过认证请求关闭对应 worker；仍活跃的客户端可能在下次心跳时重新启动它，因此主动释放模型前应先关闭或暂停客户端。命令也接受 `--directory PATH`。

模型权重本身仍占约 4 GiB，共享只消除重复副本。日常编码一次处理一条、最多 512 token 的文本，并立即释放临时缓存。MLX 的 6 GiB 内存设置是调度参考，不是操作系统的硬上限。实际测量方法见[资源探测（英文）](../native-memory-recommendation.md#model-memory-and-multiple-sessions)。更新后应重启 Astra 会话；旧后端仍持有自己加载的模型。

<a id="windows"></a>

## Windows

记录前台应用活动请看 [Windows 活动记录](windows-activity.md)，使用独立的 `.\scripts\activity-control.bat` 菜单。嵌入服务菜单只控制模型服务，不切换活动记录。

双击 `.\scripts\embedding-control.bat` 可使用 Start / Stop / Status / Exit 菜单。关闭菜单不会停止后台服务；在终端执行 `.\scripts\embedding-control.bat start`、`stop` 或 `status` 可直接完成对应操作。它是可选的 Windows 辅助入口，macOS 启动不会调用它。

默认程序和模型路径分别是 `D:\llama-cpp\llama-server.exe`、`D:\llama-cpp\models\Qwen3-Embedding-4B-Q4_K_M.gguf`。其他安装位置可通过 `scripts/start-embedding-windows.ps1` 的 `-Server`、`-Model`、`-Port` 指定；停止或检查时也使用这些参数，并加上 `-Action stop` 或 `-Action status`。停止操作核验程序、模型、嵌入参数、端口和进程创建时间，不会批量结束所有 llama.cpp 进程或任意端口占用者。

下载官方 [Qwen3-Embedding-4B GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF)。启动脚本默认使用 Q4_K_M，在回环地址的 8088 端口启动隐藏服务。它不是开机启动任务，Windows 重启后需要重新运行。服务必须使用 `--embedding --pooling last`，不能把地址指向仅提供聊天的 llama.cpp 服务。多个 Astra 会话共享这个服务；Windows 不会启动 macOS 的 MLX worker。

在本机不纳入 Git 的 `.env` 中设置：

```dotenv
ASTRA_EMBEDDING_BACKEND=llamacpp
ASTRA_EMBEDDING_BASE_URL=http://127.0.0.1:8088/v1
ASTRA_EMBEDDING_MODEL=Qwen3-Embedding-4B-Q4_K_M
ASTRA_EMBEDDING_TIMEOUT=2
ASTRA_CONTEXT_INDEX_SESSION_MS=450
ASTRA_CONTEXT_INDEX_SOURCE_MS=700
```

HTTP 客户端复用连接、绕过继承的代理设置，并检查输入顺序和向量形状；失败时退回文本推荐。需要认证的端点可设置 `ASTRA_EMBEDDING_API_KEY`。HTTP 超时和推荐汇总器的等待预算独立；嵌入服务不可用不应阻止正常对话。

<a id="data-and-enabling"></a>

## 数据准备与启用

`/context-index session` 启用本地记录和会话推荐。文本检索不需要嵌入模型；准备好语义索引后，可增加表达不同但含义相关的候选。`/context-index all` 还会启用活动推荐。

`/context-index status` 显示后端及活动数据库、向量文件是否存在，不是服务健康探测。`/context-index why` 显示最近实际检索结果和超时情况；运行过语义通道时也分别展示其结果。

模型可主动调用原生 `context_inspect`，读取当前轮的推荐数量、预算、各条目的检索通道和有效句柄。它不加载模型、不读取档案，也不改变或重新执行检索。上一轮摘要单独显示，只含元数据；每轮最多四条推荐并不会改变 `context_open` 最多打开三个句柄的限制。详情见[注入检查（英文）](../native-memory-recommendation.md#inspecting-an-injection)。

会话读取保持只读。局部匹配按共同的查询字符覆盖规则排序，会话和原生活动检索复用 trigram 索引，会话检索另用字符对索引查找两字中文词，并在限量前应用精确目标的边界条件。两端使用 Python 和 SQLite，无需新增服务；字符对表由 Session Recall 在首次使用时于自身的存档中建立。语义通道参与原有 RRF 排名融合；独立语义任务超时不会丢弃已完成的文本结果。

上面的预算参数属于本机调优，两端默认会话预算仍为 75 ms、汇总器预算为 200 ms。活动检索会在其 75 ms 读取预算内为后续独立通道预留时间，并保留中断前已完成的结果。诊断分别报告档案错误、语义错误和允许展示的阶段计时；读取超时不能直接证明嵌入后端不可用。

活动历史是独立数据。Git 同步不会复制活动档案、模型权重、本地配置或向量索引。活动数据库不存在时，该来源报告 `absent`，不会因此启动桌面记录。启用前可导入兼容档案，或设置 `ASTRA_CONTEXT_INDEX_ACTIVITY_DB`。从活动摘要建立向量：

```console
python -m agent.runtime.context_index.vector_indexer --activity-db PATH_TO_ACTIVITY_DB
```

会话和结构化记忆向量在同一模型专用向量文件中使用自己的表，不依赖活动档案或桌面记录。后端可用时，在 Windows 已激活的 `.venv` 或 macOS 环境中运行：

```console
python -m agent.runtime.context_index.semantic_indexer
```

默认每个来源最多编码 128 条有变化的记录。`pending` 非零时可继续运行，或在显式维护时指定 `--max-encode 5000`。`--sessions-db`、`--memory-db`、`--vectors-db` 可覆盖路径；`--source session` 或 `--source memory` 可限制范围。`--rebuild --max-encode 5000` 会重新编码受限档案窗口内的记录。模型标识变化时应使用独立向量文件；不同仓库或档案路径属于不同范围，单独复制向量文件不会让原证据在另一处自动可用。

已启用推荐且向量文件存在时，启动会预热所选后端，并每 30 秒维护每个来源最多 16 条变化记录。内核锁保证同一向量文件同时只有一个后台维护者，每编码一条就提交进度。维护只使用就绪后端，忙碌时查询仍可退回文本路径。首次建好索引后重启 Astra；设置 `ASTRA_CONTEXT_INDEX_EMBEDDING=off` 可仅使用文本检索。

会话/记录的余弦相似度阈值默认是 `ASTRA_CONTEXT_INDEX_MEMORY_MIN_SIMILARITY=0.75`。这是基于 Mac MLX 测试样本校准的值，不是通用概率，也不是已经验证的 GGUF 阈值。在 Windows 修改阈值前应运行公开的质量回放；活动来源阈值不受此项改变影响。

索引器加载项目 `.env`。HTTP 模型标识对应独立的默认向量文件名；即使向量维度相同，文件元数据也会拒绝模型标识不匹配。让 `ASTRA_EMBEDDING_MODEL` 与实际模型和量化版本一致，更换时使用新标识/路径。没有元数据的旧 MLX 索引仅允许由 MLX 读取，不能通过显式路径覆盖让 GGUF 后端复用它。

修改 `.env` 后重启 Astra，重新加载进程级后端和偏好。推荐管线在两个平台上均为 Astra 原生实现；查询只使用已预热实例，编码并发受限，冷启动或忙碌时使用文本路径。共享预算、反馈和回归命令见[原生记忆推荐（英文）](../native-memory-recommendation.md)。
