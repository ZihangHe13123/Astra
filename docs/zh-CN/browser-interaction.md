# 浏览器操作：CDP 与日常 Edge

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../browser-interaction.md)

[日常浏览器配置：Windows 和 macOS](#everyday-browser-setup-macos-edge) · [模型操作流程](#model-workflow) · [多个 Astra 实例](#multiple-astra-instances) · [当前限制](#current-limits) · [表单编辑与验证](#form-editing-and-verification) · [选择状态与精简观察](#choice-states-and-compact-observations) · [批量设置选择状态](#checked-choice-batches) · [更新与验证](#updating-and-verifying)

CDP 后端返回结构化页面观察，支持最新快照中的 `ref:<id>` 目标，也保留唯一 CSS 选择器。更新工具后重启 Astra。此 Chromium 集成不包含 Safari 控制。

<a id="everyday-browser-setup-macos-edge"></a>

## 日常浏览器配置：Windows 和 macOS

独立的 `browser-control-extension` 通过 Native Messaging 连接日常使用的 Edge。原有 **Astra Activity URLs** 只记录活动网址，不能操作页面；应保留它，其权限没有改变。

1. 在 Edge 扩展管理页，从稳定的 Astra 仓库加载 `browser-control-extension`（解压缩扩展），复制扩展 ID。
2. 从同一稳定仓库运行下面的安装命令，将占位符换成实际的 32 字母 ID。

```sh
astra browser-control install --browser edge --extension-id YOUR_EXTENSION_ID
```

Chrome 使用 `--browser chrome`。安装器只注册该 ID，并记录当前仓库和 Python 路径；移动仓库或环境后显式运行 `repair`。临时 worktree 和缺少扩展资源的包安装不允许安装宿主。Windows 只写所选浏览器当前用户的 HKCU NativeMessagingHosts 注册项，无需管理员权限；`%LOCALAPPDATA%\Astra\browser-control-host` 和运行时端点采用仅当前用户/SYSTEM 可访问的受保护 ACL。macOS 保留原有清单位置。

3. 更新 Python 文件后重启 Astra。安装器管理的宿主与仓库匹配时，首次浏览器任务（包括发现标签页）才会取得端点并启动监听。聊天启动仅核验配置，不占用端点、不启动 Edge，也不等待浏览器。
4. 更新扩展文件后，在 Edge 中重新加载 **Astra Browser Control**。0.2.0 版本引入 storage/alarms 权限和就绪协议，需接受扩展权限更新，并在弹窗中启用一次 **Auto-connect**。
5. 授予网站访问权限。**Allow current tab** 只授权当前这一张实际标签页；可选的 **Allow HTTP(S) sites for new agent tabs** 允许以后由 Agent 新建的标签页访问网站，不会开放所有已有标签页。
6. 直接交给 Astra 浏览器任务。`browser_open` 按需连接并打开可见的 Agent 标签页，无需先调用 `browser_connect`。Edge 关闭时会启动普通应用，不创建调试配置文件。

连接等待可取消，最多 45 秒。扩展采用短重试和 30 秒 alarm 兜底，浏览器挂起可能延迟恢复。原生宿主认证并恢复会话授权后才报告就绪。连接失败或取消不会派发页面动作；新标签页导航另有 10 秒就绪时限。跨来源重定向需要重新授权，不会因打开链接而自动授权目的地。

自动模式下，Astra 重启或短暂断连可以保留**同一浏览器会话中仍存活的已授权标签页**，前提是 ID、来源和当前网站权限核验通过；暂停的标签页继续暂停。旧逻辑句柄和元素 ref 仍失效，需要重新列出、绑定和获取快照，但无需重复弹窗授权。中断的写操作不会重放。浏览器重启或扩展重载会清除会话授权；恢复的用户标签页需要重新选择。新建 Agent 标签页的网站权限则可保留。

**Stop and revoke all tabs** 会关闭自动连接、取消重试并清空已保存和当前授权，重启后仍保持停止。只取消 **Auto-connect** 勾选则关闭未来重试和已保存授权，但允许已经连接的手动会话继续。

`ASTRA_BROWSER_TRANSPORT=cdp` 或 `manual` 保留旧版手动连接方式；`auto` 或 `extension` 显式要求自动模式，宿主缺失或不匹配时直接报告配置错误。`ASTRA_BROWSER_APP=edge` 或 `chrome` 指定应用；两个宿主都安装时优先 Edge。自动模式连接失败不会悄悄切换到 CDP，已有 CDP 绑定也保持原目标。当前不提供 Safari 和 Linux 原生宿主安装。

### 检查和修复可选集成

Windows 宿主管理命令应在你自行打开的普通 CMD/PowerShell 中运行。隔离的 Agent 或打包应用进程可能读回自己写入的注册表项，但 Edge 看不到它。安装器会拒绝已知的隔离/打包环境；不要移除检查或改写全机器注册来绕过。此前在该环境尝试过安装的，应在普通终端显式 `repair`；若受管理文件也不存在，则用实际扩展 ID 执行 `install`。

```text
astra browser-control status --browser edge --json
astra browser-control repair --browser edge
```

状态检查和 `astra doctor` 不启动宿主或浏览器。`configured` 只表示注册有效，不等于扩展已连接或标签页已授权；在 Astra 内用 `/doctor browser` 或 `/browser status` 查看就绪状态。只有原生连接、尚未恢复扩展授权时不会报告可用。弹窗保留原生连接的具体错误，不再只显示等待状态。

修复/卸载会核验归属，拒绝覆盖外部注册、手改文件、重解析点和硬链接。显式修复可以收紧旧 Windows 安装的 ACL，但不会抢占活跃端点；请先在原 Astra 会话执行 `/browser stop`，不要删除锁文件。普通操作失败会恢复原有受管理文件和注册项；未知文件需要人工排查。

Windows 使用 Python `.cmd` 启动器、二进制标准输入输出和固定模块入口，不把浏览器参数拼入 shell。宿主目录含 `%` 或换行会在安装前拒绝。若组织策略禁用命令启动器或 Native Messaging，应联系策略管理员；Astra 不会降低安全策略或偷偷切换传输方式。

真实浏览器验收：停止现有 Astra 浏览器会话，用仓库虚拟环境 Python 运行 `scripts/browser_control_smoke.py`，按提示打开临时本地页面，**只授权该标签页**。脚本核对读取、填写、一次点击、运行时重启重连且不重放写操作，以及手动撤销授权；不会访问私人页面或申请全站权限。

<a id="model-workflow"></a>

## 模型操作流程

仓库技能 [`visible-browser-cdp`](../../.astra/skills/operations/visible-browser-cdp/SKILL.md) 保留了旧名称，但内容涵盖扩展自动连接、网站/标签页授权、顺序表单操作、未知结果和显式 CDP 回退。应通过 `skill_view` 加载最新内容，不复用对话中旧的 CDP 专用指引。

需要在 Astra 终端审批时，首次操作前说明需要返回终端批准。审批引起的焦点切换与浏览器目标绑定是两回事：扩展 DOM 操作使用已绑定标签页，原生前台动作使用 Computer Use 支持的接管流程。之后的截图显示终端在前台，不能证明先前点击落在哪里。应核对工具结果和目标状态，不用激活 Edge 或发送全局按键来“补偿”审批切换。

- 用 `browser_snapshot(refresh=true)` 观察当前绑定页，不导航、不重载。`elements` 包含角色、无障碍名称、禁用状态和 ref，使用最新的精确 `ref:<id>`。
- 选择题或混合表单使用 `scope="form", include_text=false`。`groups` 保留题干，元素 `group` 指向组 `id`，组的 `ref` 可交给 `browser_read` 补充上下文。存在 `nextOffset` 时继续翻页；遇到 `nameTruncated` 不猜省略内容。文本编辑器使用 `scope="editable"`。
- `browser_check` 最多设置 20 个勾选目标，跳过已满足目标并验证最终 DOM 状态。扩展 0.3.3 区分运行中的控制器和注入页面能力；旧控制器缺少原生 check 时，新页面辅助程序可通过明确授权的点击路径完成同一目标，模型仍调用 `browser_check`。快照报告 `checkRoute`、`nativeCheck`。明确的 `unsupported_operation` 不代表选择器过期，应按恢复提示处理，不换 ref/CSS 反复重试。
- click/type/select 派发前验证目标，结果含观察状态，必要时附新快照 `after`。下一步可直接使用 `after` 中的 ref；额外刷新会使它们失效。
- `observed` 只表示观察到页面/控件变化，不代表整个任务成功；还需核对预期确认、值或导航。`no_observed_change`、`unknown_outcome` 都不是成功，写操作不能自动重试，应先观察实际页面。
- 点击后的即时观察可能早于慢导航完成。用新快照或有时限的 `browser_wait` 检查，不因表单暂未消失就再次提交。
- 快照、等待、标签页发现、状态和连接绕过 ReAct 轮内缓存。四个观察工具每轮（或每个 code-mode 程序）各最多重复读取 20 次；连接和写工具仍有重复调用保护。这样可避免把缓存 ref 当成实时刷新，也限制无界观察循环和写入重放。
- `browser_wait` 可等待元素、文本或 URL 子串，多个条件必须全部满足。仅使用已观察或明确已知的条件。超时表示条件未满足，不是成功等待；扩展回执显示请求/实际超时（最多 10 秒）、条件和 URL 是否变化。再次等待前先读返回页面。
- 来源变化需要重新授权；元素过期、替换或脱离文档需要新快照，不自动改指另一个元素。
- handoff 暂停同一扩展标签页，必须显式 resume。关闭绑定的用户标签页只解除绑定，关闭 Agent 新建的标签页则真正关掉它。

<a id="multiple-astra-instances"></a>

## 多个 Astra 实例

普通启动和状态检查不占用浏览器端点，首次浏览器操作才进入串行就绪/连接流程。同一扩展端点只有一个运行时所有者。`already owned` 中的 PID 是定位线索，应核验进程、启动时间、终端和监听端口；CPU 暂时空闲不代表可以丢弃该会话。

可继续使用占用端点的实例，或在该实例运行 `/browser stop` 主动释放控制，不必退出 Astra。`/browser status` 只检查状态，不会获取端点。切换或重置聊天也会释放旧连接和逻辑句柄；数据库记录只是历史，不能直接恢复控制。释放会等待正在派发的操作结束；若清理失败，新操作保持禁止，可用 `/browser stop` 重试。断开扩展不会释放进程锁；不要删除 `owner.lock` 或结束另一会话来抢占。诊断只展示必要 PID/端口，不输出带令牌的 `endpoint.json`。浏览器调试横幅也不能识别这把锁的所有者。

所有者释放或退出后，重新发现标签页并使用新句柄绑定。成功但列表为空意味着当前无可发现的授权标签页，不代表锁仍被占用。原有用户页面应重新 **Allow current tab** 后绑定，不悄悄另开替代表单。绑定前失败时，不能对未绑定标签页调用 snapshot/read 来恢复，即使通用错误提示建议这样做。

<a id="current-limits"></a>

## 当前限制

普通快照最多 150 个交互元素、12,000 个文本字符，总序列化预算小于 64 KiB。排除 password 和 hidden 文本输入框；文件控件只返回元数据，包括视觉上隐藏的文件控件。一般页面文本仍可能含敏感内容。支持开放 shadow root 和可访问的同来源子 frame；跨来源、不透明 sandbox frame、关闭的 shadow root 仍有限制，不能当作已完整读取。

表单快照使用更小的 10K 字符预算，并在清理/持久化阶段保留紧凑 JSON。长表单分页，题干每组最多 2,000 字符、选项名最多 1,000 字符，附截断标记。勾选验证只证明 DOM 状态，不证明应用保存或服务器持久化。

扩展不截图，因为 Chromium 可见标签页捕获不能原子绑定指定标签页；CDP 仍支持截图。扩展只开放固定操作，不提供任意 eval、cookie 或 shell。原生宿主认证用户私有 localhost 端点，连接令牌不发送给浏览器，端点最多由一个 Astra 运行时持有。

卸载原生宿主：

```sh
astra browser-control uninstall --browser edge
```

之后在 Edge 移除控制扩展，不会卸载 Activity URLs 或改变活动历史。

## 无需桌面焦点的文件选择

Browser Control **0.4.1** 支持 `browser_upload`，在已授权标签页中选择、替换或清空文件。更新后重新加载扩展并重启 Astra；不新增扩展权限。扩展重载后，已有用户标签页需重新 **Allow current tab**。

1. 调用 `browser_snapshot(tab_id=..., scope="form", role_filter="file", include_text=false)`，找到目标文件控件和新 ref。支持隐藏的原生文件输入框及可访问的同来源 iframe。
2. 调用 `browser_upload(tab_id=..., selector="ref:...", paths=["/绝对路径/文件"])`。审批显示确切文件列表和目标网站，通用网站写权限不授权读取任意文件。网站可能在选择后立即上传，因此这是向网站提供文件的操作。
3. 核对 `verified`、`files` 和 `after`。验证会读回 `input.files`，比较名称、大小、MIME 类型和顺序；它不证明服务器已收到文件，也不代表已提交。最终 Submit 保持独立。

`paths=[]` 清空选择；多文件要求控件带 `multiple`。最多 10 个普通文件，单文件 32 MiB、合计 64 MiB。首版不支持目录、跨来源 frame、关闭的 shadow root、仅接受拖放的组件。文件不匹配 `accept` 提示时在选择前拒绝；匹配提示也不代表服务器一定接受。CDP 和旧扩展控制器在读取或传输文件内容前明确返回 `unsupported_operation`。

文件经 Native Messaging 分块传输，不打开文件面板、不激活应用、不点击控件、不提交表单。导航、撤销授权、handoff、控件替换或传输超时会废弃未完成的传输。commit 结果不确定时先观察文件元数据，不重放。`browser_fill`/`browser_type` 继续拒绝文件输入框。

清空导致控件重建时，仅在原文档、授权、表单和唯一字段身份仍匹配的情况下，只读核验新控件的空列表，并标记 `verification_source: replacement_input`。不会补发任何输入；目标有歧义或授权已撤销时仍保留未确认状态。

<a id="form-editing-and-verification"></a>

## 表单编辑与验证

先调用 `browser_snapshot(scope="editable")`。快照包含可访问的同来源 frame 树（包括嵌套 about:blank/srcdoc）、元素 `frameRef`、名称/上下文、editable/readonly 标记和当前值。`role_filter`、`frame_ref`、`offset`、`limit` 在元素限额前缩小范围。普通快照优先可编辑字段。frame 文档、根或 URL 代际变化会使引用失效；元素 ref 还会因重新快照、替换、移除、导航或撤销授权而失效。

使用 `browser_fill(selector="ref:<returned ref>", text="complete value")` 填入完整内容，再使用 `after` 的新 ref。`browser_read` 只读一个目标且不使 ref 失效。CSS 必须在所有可访问 frame 中唯一匹配，不采用首个匹配。`browser_type` 仍是替换语义并核对目标。包括观察在内的操作按标签页串行；排队后过期的 ref 在修改前失败，不改指其他字段，不同标签页相互独立。

原生输入 setter 派发 input/change。contenteditable 使用目标所属文档的 selection 和原生编辑命令，插入转义后的纯文本及明确换行；不使用 TinyMCE.activeEditor、全局粘贴、系统按键、浏览器激活或调试器焦点模拟。标签页和 Edge 在后台时，页面仍可能需要内部元素焦点。

`verified` 表示写入后目标值匹配，不代表服务器已经保存。应另外确认应用保存状态或响应。未知派发、过期/歧义目标和验证失败都是结构化错误，不自动重放。跨来源和不透明 sandbox frame 仍排除在外，未新增扩展权限。

从仓库根目录运行自动检查。命令明确列出文件；在这里使用的运行环境中，`node --test browser-control-extension/tests/` 不是等价调用：

```sh
npm install --prefix /tmp/astra-browser-test-deps jsdom@26.1.0 tinymce@6.8.6
NODE_PATH=/tmp/astra-browser-test-deps/node_modules node --test \
  tests/test_browser_page.cjs \
  browser-control-extension/tests/control.test.mjs \
  browser-control-extension/tests/worker.test.mjs
.venv/bin/python -m pytest -q \
  tests/test_browser_auto_connect.py \
  tests/test_browser_backend_router.py \
  tests/test_browser_control_installer.py \
  tests/test_browser_control_transport.py \
  tests/test_browser_fallback.py \
  tests/test_browser_interaction_tools.py \
  tests/test_browser_page.py \
  tests/test_browser_regression.py \
  tests/test_browser_session.py \
  tests/test_browser_tools.py \
  tests/test_cdp_backend.py \
  tests/test_extension_browser_backend.py \
  tests/test_tool_approval_scopes.py \
  tests/test_dynamic_tool_registry.py
```

更新检查项时，应调查失败并说明覆盖范围变化。独立渲染器验收每次使用新输出名，避免旧进度/报告冒充新结果：

```sh
python3 scripts/browser_frame_acceptance.py \
  --vendor-dir /tmp/astra-browser-test-deps/node_modules/tinymce \
  --output /tmp/astra-frame-acceptance-run-1.json
```

在 Edge 打开输出的回环网址并开始测试，切到另一个标签页，同时把另一个应用放到前台。样本对四个同名 TinyMCE iframe 编辑器运行 20 轮，核对多行文本、目标回读、其他字段不变、编辑器模型与隐藏表单同步，以及每次填入后的 hidden/unfocused 状态。它测试真实共享页面辅助程序，已安装扩展/原生通信需要单独验收。测试后台行为前解除其他会模拟焦点的调试器。

等待时检查本次 `.progress.json`，要求同时满足 `visibilityState === "hidden"` 和 `hasFocus() === false`。返回终端审批可能只让文档失焦，浏览器选中标签页仍可见。应按指示切换已观察到的其他标签页，再让另一应用置前。不反复点击 Start，也不忽略条件一直等待。缺少进度本身不能证明动作未派发；先观察并尊重未知结果。只停止本次启动的测试服务器，不按脚本名结束所有进程。

<a id="choice-states-and-compact-observations"></a>

## 选择状态与精简观察

快照、读取结果及动作 `after` 单独报告实时 `checked`，不依赖表单 `value`。原生输入返回布尔值，原生 checkbox 另含布尔 `indeterminate`；ARIA checkbox 返回 true、false、`"mixed"` 或 null，ARIA radio 返回 true、false 或 null。null 表示属性缺失或无效，非选择控件不含这些字段。仅 ARIA 属性变化也属于观察到变化，但不证明服务器保存，仍遵守原有点击结果及禁止重放语义。

读过题干后，可用 `browser_snapshot(scope="editable", include_text=false)` 或配合角色/frame 筛选。返回 `text=""`、`textIncluded=false`，但保留值、勾选状态、frame 上下文、新 ref、分页和限制。之后 fill/click/select 的 `after` 保留此设置；frame 消失时退回全页观察，同时仍保留文本设置。页面失效会清理设置，不同页面互不影响。

默认 `include_text=true`。精简缓存后请求文本会获取新完整文本，不返回旧的空文本。读取新内容或最终保存提示时可恢复此选项；条件等待仍读取页面文本。精简不会取消来源检查、ref 验证、目标回读或按页串行。

同一本地样本服务器的 `/tests/fixtures/browser-observation-acceptance.html` 提供 **Run observation acceptance**，可检查两个真实 TinyMCE 编辑器、原生/ARIA 选择状态、精简 `after`、过期 ref、文本恢复及代表性负载至少 40% 的缩减。报告属于渲染器证据，不是已安装原生通信验收，也不测量后台可见性/焦点条件。

<a id="checked-choice-batches"></a>

## 批量设置选择状态

`browser_check(selector="ref:<fresh>", checked=true)` 设置唯一的 radio/checkbox；独立选择可用 `checks=[{"selector":"ref:<fresh>","checked":true}, ...]`，一次 1–20 项。共享页面辅助程序同时支持扩展和 CDP。

预检会在任何点击前拒绝歧义、禁用、不支持、过期或相互冲突的原生 radio 目标。每个需要变化的目标最多点击一次并限时观察；后项失败不会重放前面成功的目标。结果包含 `verified`、`completed`、`failedIndex`、`clickCount`、各项目结果、`dispatch_state`、`verification_state` 和一份精简 `after`。已满足目标的点击数为零。checked 结果不证明保存或提交。

快照通过 `capabilities.check`、`checkBatchLimit` 声明能力。click/type/select 仍可用。传输层将 check 视为写操作，断连/超时不确定时报告未知结果，不重放。

<a id="updating-and-verifying"></a>

## 更新与验证

更新后按需重启 Astra 并重新加载控制扩展。单独核对实际加载的扩展版本，不仅看源码 manifest；重载可能使标签页授权失效。以返回的能力为准，不假定所有已安装版本能力相同。

自动检查与渲染样本覆盖协议、引用和表单行为，新的已安装扩展/原生宿主测试覆盖另一条路径。把准确版本和各通道结果记录到 `output/`。样本通过和 DOM 回读都不能单独证明真实应用已保存或提交数据。
