# Astra macOS Computer Use 实机验收 Runbook

> 将本文件完整交给 Astra 执行。目标是收集当前机器、当前提交、当前应用版本的实机证据，不是证明“所有 macOS 应用都兼容”。

## 1. 任务目标

从当前 Astra 仓库根目录依次验证（下方以 `~/astra` 为示例路径）：

1. 签名 helper 的确定性门禁；
2. AppKit Fixture 的 release AX `element_ref` click 和 release pointer fail-closed；
3. 与 release 分离的实验性 PID-pointer 五场景诊断；
4. WPS Writer 的编辑、另存副本、导出 PDF 和安全清理；
5. VS Code 与 Microsoft Edge 的发现、精确锁窗、截图和未配置能力的默认拒绝行为；
6. 整个过程中真实鼠标不被 Astra 静默移动，失败或未知结果不被重放。

只生成测试证据，不生成或注入 Fixture 的生产兼容 cell，不修改
`config/macos_computer_compatibility.json`。Task 10 只提交测量后的 transactional
matrix 更新；不提交实现或 registry 变更，也不 push。生产 registry 唯一允许的状态是
已提交的 checked-in HEAD 版本（`git diff --quiet HEAD -- config/macos_computer_compatibility.json`）；
本轮测试不得引入任何未提交改动。

## 2. 绝对安全规则

- 只能操作本轮测试创建的临时文件、Fixture 窗口和明确打开的测试窗口。
- 不打开、编辑、保存、覆盖、移动或删除用户已有文档。
- 不接受或代替用户处理 macOS 权限提示、登录、密码、Touch ID、支付、管理员授权或系统对话框。
- 不使用剪贴板，不使用全局 HID fallback，不移动或隐藏真实鼠标。
- 每个 action 使用一个新鲜 snapshot；不得跨 snapshot 重用 `element_ref`。
- 普通只读使用 `computer_apps` → `computer_get_app_state`；这不 focus app，也不移动真实 pointer。`computer_focus` 只用于明确的高级锁定/可操作流程。
- `unknown_outcome`、`user_activity_paused`、输入可能已经发生、窗口所有权不明确时，立即停止该 cell，禁止重放。
- 用户移动鼠标、点击、滚动或键入时，立即让出控制；未经用户明确要求，不自动 resume。
- 无法证明窗口、文件或提示框属于本轮测试时，不自动关闭或清理。
- `skip`、权限缺失、应用未安装、旧产物、旧日志、fail-closed、实验性记录都不是 release PASS。
- 一个 action 的 PASS 不能推导同一应用其他 action PASS；一个版本的结果不能推导其他版本。

## 3. 结果词汇

每个测试只能使用以下结果之一：

- `PASS`：本轮获得该 release 门禁的全部必需证据；实验性 PID pointer 结果不得使用这个状态。
- `NOT_RUN`：本轮未执行；退出 0 的显式 skip 仍是 `NOT_RUN`，不是 PASS。
- `BLOCKED_REAL_E2E`：权限、应用、环境或 ScreenCaptureKit discovery 阻止真实机门禁在输入前完成。
- `NOT_SUPPORTED`：实验性 PID-pointer 路径在本机不能满足验收条件；与 release 失败不同，也不是 PASS。
- `EXPERIMENTAL_DIAGNOSTIC_ONLY`：实验性五场景意外全部为正；只触发人工复核，不是 release 或 production compatibility PASS。
- `UNKNOWN_OUTCOME`：输入可能已经开始，但无法证明最终效果；禁止重放。
- `POSTCONDITION_FAILED`：action 已确认，但流程拥有的精确后置条件没有成立；必须 handoff 且禁止重放。该状态阻止 release `PASS`。
- `FAIL_CLOSED`：在输入前安全拒绝，且能证明没有输入或副作用。
- `FAIL`：执行完成但证据不满足，或出现错误副作用。

总体结果按最差 release cell 报告，实验性结果单列，不能用部分 PASS 写成“Computer Use 已全面兼容”。

## 3.1 Transactional app-state boundary

Task 8 的确定性 `computer_apps` → `computer_get_app_state` contract covers one no-focus/no-pointer read transaction; it is not a Task 10 live result. For every live row, take exact refs only from the latest catalog, then call `computer_get_app_state`. Do not use `computer_focus` for ordinary reads: it remains an explicit advanced operation. `computer_snapshot` only refreshes an already bound target.

Record the returned PNG, ordinary AX, and optional Smart detail as one transaction. Target or action invalidation clears all snapshot/action authority; a Smart approval is one-time and target-bound. Bounded errors are fail-closed. On `unsafe_artifact`, call `computer_close`, start a fresh session, and restart with new `computer_apps` and `computer_get_app_state` calls. Never reuse old refs, an old artifact, or a partial result.

The Task 10 live operator must copy the [Task 10 transactional app-state matrix](macos-app-state-acceptance.md) into an ignored local evidence directory and populate that local copy. Do not commit the populated matrix or raw run metadata. It is the canonical per-cell evidence for the local run. The final report must reference this local matrix as its input, not create a competing output. Public documentation may contain only a redacted summary of the measured behavior and limitations.

### 3.2 前台礼让：仅在真人同意后验收

以下不是自动门禁。使用测试窗口，记录提交、客户端与终端版本；没有真人执行的行标 `NOT_RUN`。

| 动作 | 应记录的结果 |
| --- | --- |
| GUI 用 Tab 聚焦「查看后端提供的审批详情」，按下、松开 Enter，再做一次 | 详情展开、折叠；请求仍待决定，批准数和接管数均为零。另用真正的允许按钮作正对照。 |
| TUI 在支持松键上报的终端按住再松开 Y、Enter 或已提供的 A；再在仅上报按下的终端试一次 | 前者只在同一选择松键后批准；后者保持待决定，可以拒绝。松键后另打一键或切走应让行，不能用延时忽略这次输入。 |
| 用非 debug 的 TUI 和只记录答复、不执行工具的测试后端；在输入框连续打字时送入普通审批，再让一张普通审批暂时盖住 `/connect` | 卡片和选项写出前，原本输入的 Y 不得产生批准。卡片可见后 Enter 只回答该卡；连接面板重新显示后独立按 Enter 才能选路。不要实际连接账号。 |
| 用同一张假发送卡（主收件人、抄送、密送、主题、六行不同正文），对照 50×14 与 120×36，再调整窗口大小 | 核对卡片和输入：内容不全时 Y 不发送，N 可丢弃；Ctrl+Y 不是草稿快捷键，预开或另处开启 YOLO 也不能结算本卡。所有字段完整写出后须重新按 Y 或 Enter。只记录决定，不发邮件。 |
| 用标记为 `truncated:true` 的测试草稿重放给 GUI，分别试有正文节选和无正文的卡 | 都不提供发送按钮或肯定快捷键，可以丢弃。完整草稿须列出实际提供的主收件人、抄送、密送。 |
| 用无传输的 MCP 替身登记 `…/replies.post`、同一路径的 GET、文件上传和 PR 合并 | 回复卡核对完整正文及仓库、线程等实际目标；GET 不误报为发送。`file_uri` 或仓库名加数字 PR 号得到普通单次审批，不要求邮件正文。只记录决定，不发帖、上传或合并。 |
| 在模拟连接页显示测试路由 A，更新为 X，或将选择移到另一个同名路由；另对照 60×34 和 65×34 | 屏幕仍显示 A 时按 Enter 不提交。选中标签和 ID 可分行，全部写出后须重新按 Enter 才可记录 X；不进行网络登录。 |
| 在慢终端或复用器中，用同一记录后端观察旧卡撤走时尚未收完的方向键、终端回包和带标记粘贴 | 旧终端的独立 Esc 超时最多拒绝原卡；一次分片输入不能再回答下一卡。留存时间和请求编号，不记录真实粘贴内容。 |
| 在输入框键入 `const ids = [1`，单按 Esc、稍停，再补 `];` 和回车；让记录后端给出下一张普通卡 | 输入框恰好提交一次 `const ids = [1];`，下一张卡能独立拒绝或取消。终端控制串或复位键本身都不是批准。 |
| 请求展示测试文档 A，让应用在收尾时自行聚焦同进程文档 B；另在已聚焦的测试 sheet 中完成操作 | 不能把 B 报成 A；`AXFocusedWindow` 返回父窗口时，必须用当前字段归属证明是原 sheet。不能为了验证重新激活。 |
| 已确认勾选动作，但新截图显示未选中，归还原应用又明确失败 | 分开记录输入、截图、勾选状态和前台：`presentation_status=unconfirmed`，不重放点击。不便安全制造失败时用确定性测试，实机项不记 PASS。 |

## 4. 建立本轮证据目录

从仓库根目录运行：

```bash
cd ~/astra
umask 077
astra_run_id="$(date +%Y%m%d-%H%M%S)"
astra_evidence_dir="/tmp/astra-computer-acceptance-${astra_run_id}"
mkdir -m 700 "$astra_evidence_dir"
cp docs/macos-app-state-acceptance.md "$astra_evidence_dir/app-state-matrix.md"
printf 'run_id=%s\nevidence_dir=%s\n' "$astra_run_id" "$astra_evidence_dir"
git rev-parse HEAD | tee "$astra_evidence_dir/git-head.txt"
git status --short --branch | tee "$astra_evidence_dir/git-status-before.txt"
shasum -a 256 config/macos_computer_compatibility.json \
  | tee "$astra_evidence_dir/registry-before.sha256"
```

要求：

- 记录 HEAD 和现有 dirty files，但不修改或清理它们。
- 后续只把日志写入 `$astra_evidence_dir`。
- 后续 shell 代码块必须在同一个持久 shell 会话中运行；如果工具每次启动新 shell，就把本轮实际 evidence directory 的绝对路径替换进 `$astra_evidence_dir`，不得重新生成 run ID。
- 不设置任何 screenshot retention/debug retention 环境变量。

## 5. 阶段 A：确定性和签名前置门禁

依次运行，保存完整 stdout/stderr 和退出码：

```bash
set -o pipefail
git diff --quiet HEAD -- config/macos_computer_compatibility.json
astra_registry_preflight_unchanged=$?
if [[ "$astra_registry_preflight_unchanged" -ne 0 ]]; then
  printf 'production registry differs from checked-in HEAD; refusing real-machine actions\n' >&2
  exit "$astra_registry_preflight_unchanged"
fi

bash scripts/test_macos_computer_helper.sh \
  2>&1 | tee "$astra_evidence_dir/native-gates.log"
astra_native_status=$?

.venv/bin/python -m pytest \
  tests/test_computer_tools.py \
  tests/test_computer_backend.py \
  tests/test_computer_protocol.py \
  tests/test_macos_computer_helper_integration.py \
  tests/macos_computer_e2e/test_cooperative.py \
  tests/macos_computer_e2e/test_wps.py \
  -q 2>&1 | tee "$astra_evidence_dir/python-contract-gates.log"
astra_python_status=$?

bash scripts/build_macos_computer_helper.sh \
  2>&1 | tee "$astra_evidence_dir/helper-build.log"
astra_build_status=$?

codesign --verify --deep --strict --verbose=2 \
  .astra/bin/AstraMacComputerHelper.app \
  2>&1 | tee "$astra_evidence_dir/codesign.log"
astra_codesign_status=$?

cmp config/macos_computer_compatibility.json \
  .astra/bin/AstraMacComputerHelper.app/Contents/Resources/macos_computer_compatibility.json
astra_registry_cmp_status=$?

nm -u .astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper \
  | python3 scripts/check_macos_computer_helper_symbols.py \
  2>&1 | tee "$astra_evidence_dir/symbol-gate.log"
astra_symbol_status=$?

printf 'registry_matches_head=%s native=%s python=%s build=%s codesign=%s registry_cmp=%s symbols=%s\n' \
  "$astra_registry_preflight_unchanged" "$astra_native_status" "$astra_python_status" \
  "$astra_build_status" "$astra_codesign_status" \
  "$astra_registry_cmp_status" "$astra_symbol_status" \
  | tee "$astra_evidence_dir/preflight-status.txt"
```

PASS 条件：

- Swift Testing 实际发现并通过非零测试；helper harness 和 Python Computer Use contract suite 通过；
- preflight `git diff --quiet HEAD -- config/macos_computer_compatibility.json` 退出 0；registry 可以非空，必须与本轮 checked-in HEAD 一致，且不得把 configured cell 当作本轮实机 PASS；
- helper build 和 deep/strict codesign 退出 0；
- source registry 与签名 bundle resource 字节完全相同；
- helper 同时包含 `_CGEventPostToPid`、`_CGEventTapCreate`；
- helper 不包含精确 `_CGEventPost`、`_CGWarpMouseCursorPosition`。

任一条件失败：整体记为 `BLOCKED_REAL_E2E` 或 `FAIL`，不要执行实机输入阶段。

## 6. 阶段 B：权限只读预检

不要打开 System Settings，不点击权限提示。只运行：

```bash
helper=.astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper
printf '%s\n' \
  '{"protocol_version":4,"request_id":"manual-e2e-preflight","operation":"status","payload":{}}' \
  | "$helper" \
  | tee "$astra_evidence_dir/helper-status.jsonl"

swift -e 'import CoreGraphics; print(CGPreflightPostEventAccess() ? "true" : "false")' \
  | tee "$astra_evidence_dir/cgevent-preflight.txt"
```

继续条件：

- status 明确包含 Accessibility `true` 和 Screen Recording `true`；
- CGEvent posting preflight 为 `true`。

否则记录 `BLOCKED_REAL_E2E`，说明缺少哪项权限，停止；权限缺失不能算兼容失败或 PASS。

## 7. 阶段 C：AppKit Fixture release 双门禁与实验诊断

### 7.1 Release AX 与 pointer fail-closed 门禁

先确保用户当前真实鼠标停在安全位置，然后运行：

```bash
set -o pipefail
ASTRA_MACOS_COMPUTER_E2E=1 \
  bash scripts/test_macos_computer_actions_e2e.sh \
  2>&1 | tee "$astra_evidence_dir/fixture-release.log"
astra_fixture_release_status=$?
printf 'fixture_release_exit=%s\n' "$astra_fixture_release_status" \
  | tee "$astra_evidence_dir/fixture-release-status.txt"
```

退出 0 和精确终止行
`AstraMacComputerE2EHarness: PASS AX-first/fail-closed` 直接证明以下运行时证据：

1. **Release AX gate**：对 `Fixture button` 使用 fresh snapshot 中的 `element_ref`，不是坐标；执行后的 fresh snapshot 必须包含 `astra.click_count:1`。sentinel 在 action 前后都保持 frontmost，物理 cursor 坐标完全不变。
2. **Release pointer fail-closed runtime evidence**：同一按钮的 coordinate click 分别以 `background` 和 `foreground_takeover` planning 请求，二者都返回 `background_action_unsupported`，且没有 summary 或 `plan_ref`；sentinel 在 planning 前后保持 frontmost，物理 cursor 坐标不变。

完整的 **release pointer fail-closed gate** 还要求阶段 A 的 native/Python contract regressions 通过。它们证明 production composition 固定使用 unavailable PID-pointer capability，planning failure 发生在 plan storage 和 approval 之前，且无 `plan_ref` 时不能进入 focus activation、takeover、sidecar launch 或 input 路径。`approval 请求数为零`、`未调用 focus/takeover/sidecar/input 路径` 是 contract regression 与控制流证据；release marker 本身只观测 action 前后的 frontmost PID 和 cursor，不声称提供连续 focus trace 或 sidecar-launch counter。

`action_result.status=action_acknowledged` only proves that the guarded native
action was accepted. `effect_verification=unverified` means the attached fresh
snapshot must be inspected and a flow-owned exact postcondition must pass before
the action may be called effective.

WPS Home recent-document rows are not part of the supported complete workflow.
An acknowledged row `AXPress` is attempted once, then the exact expected WPS
document window is polled. If it does not appear, record
`POSTCONDITION_FAILED`, hand off, and do not replay or enable PID pointer
fallback. The supported edit/save/export gate opens its test-only DOCX through
the exact system path.

缺少任一证据都不能使用 `PASS`。如果 Accessibility、fixture 或 ScreenCaptureKit 无法在输入前建立精确目标，记录 `BLOCKED_REAL_E2E`。如果 action 可能已经投递但证据不全，记录 `UNKNOWN_OUTCOME` 并停止；禁止重放。

ScreenCaptureKit discovery 仍有独立的间歇性失败边界。若本轮在输入前以 `targetGone` 或等价 discovery 错误停止，保留完整日志并记 `BLOCKED_REAL_E2E`；只允许把整条 release 命令作为一轮全新调用重跑，不能重放单个 action。若无法证明失败发生在输入前，则记 `UNKNOWN_OUTCOME`，不得自动重跑。

### 7.2 实验性 PID-pointer 五场景矩阵

实验入口与 release 门禁严格分离。两个环境开关只选择实验，不代替真人同意。请在已授权的 Mac 前台终端运行：初始化 fixture 和每个需要前台的计划会分别显示本次挑战文字，输入后按提示松开 Return，再单独按下并松开右 Shift 才批准该次操作。不要用脚本或管道代答。

```bash
set -o pipefail
ASTRA_MACOS_COMPUTER_E2E=1 \
ASTRA_MACOS_COMPUTER_PID_POINTER_EXPERIMENT=1 \
  bash scripts/test_macos_computer_pid_pointer_experimental.sh \
  2>&1 | tee "$astra_evidence_dir/fixture-pid-pointer-experimental.log"
astra_fixture_experimental_status=$?
printf 'fixture_experimental_exit=%s\n' "$astra_fixture_experimental_status" \
  | tee "$astra_evidence_dir/fixture-pid-pointer-experimental-status.txt"
```

它可能收集 `ordinary_button`、`double_click_surface`、`scroll_surface`、`drag_surface`、`custom_canvas` 五条 `ASTRA_COOPERATIVE_MATRIX_RECORD`，仅用于诊断测试注入的 PID-targeted backend。当前主机的既有新鲜结果是终止行
`AstraMacComputerE2EHarness: NOT_SUPPORTED experimental PID pointer matrix`、exit 78；未新鲜重跑时只能把它报告为既有 `NOT_SUPPORTED (78)` 证据。

没有交互终端、未批准、超时或批准时目标改变，会以 `NOT_RUN`、exit 77 停止余下场景。这不是兼容性失败或通过，也不抹去此前已执行的场景。若新鲜实验运行仍为 exit 78，报告 `NOT_SUPPORTED (78)`；若意外得到
`EXPERIMENTAL_PASS PID pointer matrix` 和 exit 0，只能报告
`EXPERIMENTAL_DIAGNOSTIC_ONLY (0)` 并升级给人工复核，不能写成 release `PASS`。其他非零退出或输入后证据缺失按 `UNKNOWN_OUTCOME`/`FAIL` 原样报告。

这些记录和测试注入 registry cell 都不是受支持的生产兼容结果，不得生成 `accepted_applications` 候选、写入正式 registry、转写成 release `PASS`，也不得把 `NOT_SUPPORTED` 算作 release 门禁失败。本 runbook 的 release 验收不要求再次运行实验入口。

## 8. 阶段 D：WPS Writer 完整测试专属文件流程

前置要求：WPS Office 已安装且可以正常启动。不要预先打开用户文档。WPS readiness/文档流程与 Fixture release 门禁独立，Fixture `PASS` 不能替代 WPS 本轮证据。

运行仓库现有的单个真实 WPS 测试：

```bash
set -o pipefail
ASTRA_MACOS_COMPUTER_E2E=1 \
  .venv/bin/python -m pytest \
  tests/macos_computer_e2e/test_wps.py::test_wps_edit_save_copy_and_export_uses_exact_path_approvals \
  -vv -s \
  2>&1 | tee "$astra_evidence_dir/wps-e2e.log"
astra_wps_status=$?
printf 'wps_exit=%s\n' "$astra_wps_status" \
  | tee "$astra_evidence_dir/wps-status.txt"
```

WPS PASS 必须同时证明：

- 使用本轮新建、mode-0700 临时目录中的 `wps-source-<随机>.docx`；
- 文档由 `ASTRA_WPS_ORIGINAL` 编辑为 `ASTRA_WPS_EDITED`；
- 另存副本和 PDF 都位于同一测试专属目录；
- 新生成 DOCX 和 PDF 均可提取到 `ASTRA_WPS_EDITED`；
- 本测试观测到的 high-impact destination approvals 的 choices 均为 `['once', 'deny']`；
- 这些 approvals 中的已知路径集合精确等于本轮副本和 PDF 路径，且 scope 使用 `computer-write-batch:` 前缀；
- 未使用用户已有文档、目录或旧产物；
- cleanup 只关闭本轮文件/提示框，未关闭用户无关窗口；
- 没有 `unknown_outcome`，没有重放 action。

若 WPS 在 AX tree preparation、选择或 snapshot 阶段失败且能证明没有输入，记 `BLOCKED_REAL_E2E` 或更精确的 `FAIL_CLOSED`。若输入后无法证明结果，记 `UNKNOWN_OUTCOME` 并停止；不要为“跑通”而手动补点、补按键或重跑同一步。

## 9. 阶段 E：VS Code 与 Microsoft Edge 实机 readiness probe

正式 registry 已包含精确版本的能力 cell（例如 VS Code 1.134.0 的 pid_pointer click/scroll），不能再假定所有 pointer 都会拒绝。以本轮 checked-in registry 和 helper bundled registry 为准，分别记录 configured 与 fresh live evidence。本阶段仍是只读 readiness/default-deny probe；已有配置的 action 不在此阶段试发，应转入明确授权、具有效果后置条件的独立测试。详见[证据与路由约定](computer-use-evidence.md)。

分别对下列应用执行完整独立 cell：

| Application | Expected bundle ID | 先前版本仅作参考，必须读取本轮实际版本 |
| --- | --- | --- |
| Visual Studio Code | `com.microsoft.VSCode` | `1.134.0` |
| Microsoft Edge | `com.microsoft.edgemac` | `151.0.4129.107` |

先创建并打开测试专属内容；如果应用未安装，记录 `BLOCKED_REAL_E2E`，不要换成其他应用：

```bash
mkdir -m 700 "$astra_evidence_dir/app-probes"
printf '%s\n' 'ASTRA_VSCODE_COMPUTER_PROBE' \
  > "$astra_evidence_dir/app-probes/astra-vscode-probe.txt"
printf '%s\n' \
  '<!doctype html><meta charset="utf-8"><title>Astra Edge Probe</title><button>Astra probe button</button><div style="height:2000px">Scroll probe</div>' \
  > "$astra_evidence_dir/app-probes/astra-edge-probe.html"
chmod 600 "$astra_evidence_dir/app-probes/"*
open -a "Visual Studio Code" "$astra_evidence_dir/app-probes/astra-vscode-probe.txt"
open -a "Microsoft Edge" "$astra_evidence_dir/app-probes/astra-edge-probe.html"
```

只允许选择标题或路径能证明属于上述两个测试文件的窗口。若应用复用现有窗口且无法区分测试 tab 与用户内容，记 `BLOCKED_REAL_E2E`，不要操作。

对每个应用，让 Astra 严格执行：

1. 使用 `computer_apps` 读取本轮实际 bundle ID、版本、PID 和可选窗口；应用缺失记 `BLOCKED_REAL_E2E`。
2. 只选择一个标题明确、非登录、非设置、非权限提示的普通窗口。
3. 使用 `computer_get_app_state` 获取该 latest-catalog ref 的 fresh target-window observation；不得依据标题猜测另一个窗口，也不得 focus 或移动 real pointer。
4. 仅在已显式 `computer_focus` 的高级流程中使用 `computer_snapshot` refresh；记录 snapshot 成功或精确失败码。
5. 若 snapshot 成功，从 AX tree 选择一个非敏感、测试专属、不会写入用户数据的候选；没有这种候选则不执行 action。
6. 先核对精确 bundle/version/backend/action。只有该组合确实未配置，且动作无法经 AX-native 路径执行时，才可对测试专属目标做 default-deny probe，预期输入前返回 `background_action_unsupported` 或等价拒绝。已配置或路由不能确认时不发送输入，记录 configured 与 `NOT_RUN`，不故意用真实输入验证“应该拒绝”。
7. 对实际执行的拒绝 probe，记录 `last_acknowledged_action == -1`、无成功 outcome、目标内容无变化、真实鼠标坐标无变化。未发 probe 的行保持 `NOT_RUN`。
8. 调用 `computer_close` 结束该应用 cell；不关闭应用本身或用户窗口。

以下任一情况都必须记 `FAIL_CLOSED`，不能记 PASS：

- AX tree preparation/selection 失败；
- exact window identity 无法证明；
- snapshot stale/target gone；
- 已执行 probe 且 pointer cell 未被正式 registry 配置，helper 安全拒绝。

应用版本与旧报告不同只会使旧版本证据失效，不是 `FAIL_CLOSED`。继续测试时必须记录本轮精确版本并对该版本执行全新的 discovery/snapshot/default-deny readiness probe，且结果仍不能升级为 pointer compatibility。

如果 pointer action 实际发生、鼠标移动、错误窗口变化或回执包含未知 outcome，记 `UNKNOWN_OUTCOME` 或 `FAIL`，立即停止。

本阶段是与 WPS、Fixture 分离的 readiness boundary。正确成功表述只能是：

> “该应用的 discovery/snapshot/default-deny readiness probe 通过；没有获得 pointer compatibility PASS，也没有生成可接受 cell。”

## 10. 用户输入中断专项检查

只在 Fixture release AX gate 已 PASS 且用户明确准备配合时执行一次：

1. Astra 生成一个包含多个 pointer action 的已批准 Fixture fragment。
2. 在第一个 action 后，用户主动小幅移动真实鼠标或按普通键。
3. Astra 必须在下一 action boundary 停止，返回 `user_activity_paused` 或安全等价结果。
4. 已开始的输入必须完成 paired release；后续 action 不得执行。
5. 同一 plan、snapshot、takeover ref 不得重放。
6. Astra 调用 `computer_close`，等待用户决定是否开始全新的 session。

若无法确保使用 Fixture 和测试专属状态，则该专项记 `NOT_RUN`，不要在 WPS、VS Code 或 Edge 上尝试。生产 registry 无未提交改动且实验性 PID-pointer matrix 为 `NOT_SUPPORTED` 时，本专项保持 `NOT_RUN`；实验结果不能作为执行此专项的受支持 exact cell。

## 11. 结束核验

```bash
git diff --quiet HEAD -- config/macos_computer_compatibility.json
astra_registry_final_unchanged=$?

shasum -a 256 config/macos_computer_compatibility.json \
  | tee "$astra_evidence_dir/registry-after.sha256"
cmp "$astra_evidence_dir/registry-before.sha256" \
  "$astra_evidence_dir/registry-after.sha256"
astra_registry_unchanged=$?

git status --short --branch \
  | tee "$astra_evidence_dir/git-status-after.txt"

printf 'registry_unchanged=%s registry_matches_head=%s\n' \
  "$astra_registry_unchanged" "$astra_registry_final_unchanged" \
  | tee "$astra_evidence_dir/final-status.txt"
```

阶段 A 的断言防止未提交的 production registry 改动影响任何真实机 action；本阶段再次运行同一断言，只有 `astra_registry_final_unchanged=0` 时，才能声称 production registry 在本轮结束时仍与 checked-in HEAD 一致。hash/cmp 只证明字节未变化，不能替代 git diff 断言。`registry-before.sha256` 与 `registry-after.sha256` 的文件路径字段相同，正常情况下 `cmp` 应退出 0。不得为了让它通过而修改或还原用户的其他 dirty files。

确认没有本轮遗留的 Fixture、E2E harness 或测试专属 WPS 窗口。只清理能够证明属于本轮 run ID 的对象；所有权不明确时报告残留，不自动处理。

## 12. 最终报告格式

Task 10 must first populate the local copy of the [transactional app-state
matrix](macos-app-state-acceptance.md).
The final-report tables below summarize its rows and link their local evidence;
they do not duplicate or replace the matrix. Keep this report in ignored local
storage. Only a redacted summary belongs in the public repository.

将报告保存为：

```text
output/computer-use/<YYYY-MM-DD-HHMM>-real-machine-report.md
```

报告必须包含：

````markdown
# macOS Computer Use Real-Machine Report

- Date/time/timezone:
- Git HEAD:
- macOS version:
- Helper codesign identity/result:
- Accessibility:
- Screen Recording:
- CGEvent posting preflight:
- Evidence directory:
- Registry matches checked-in HEAD: exit 0
- Helper build_id / helper_git_revision / helper_source_dirty:
- Bundled compatibility_registry_sha256:
- Configured capabilities versus fresh live results: separate evidence rows
- Registry SHA-256 before/after:
- Registry changed: no

## Deterministic gates

| Gate | Exit | Result | Evidence file |
| --- | ---: | --- | --- |

## Release real-machine gates

| Gate | Bundle ID | Exact version | Result | Input started | Cursor unchanged | Sentinel frontmost | Fresh effect | No plan/approval/sidecar | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## Experimental PID-pointer diagnostic

- Switches enabled: yes / no
- Exit: 78 / other / NOT_RUN
- Result: NOT_SUPPORTED / NOT_RUN / EXPERIMENTAL_DIAGNOSTIC_ONLY / UNKNOWN_OUTCOME / FAIL
- Five scenario records observed: list; diagnostic only
- Production compatibility inferred: no

## Unknown outcomes and interruptions

- None, or list every occurrence and confirm no replay.

## Production compatibility registry

Record the checked-in `config/macos_computer_compatibility.json` SHA-256 and the
signed helper's bundled registry SHA-256. List only the exact cells relevant to
this run. Registry presence means configured, not a fresh real-machine PASS;
Fixture tests, earlier measurements and this run's app effects remain separate.
Use the [evidence row format](computer-use-evidence.md) for the distinction.

The production registry was not modified during this run; it matches checked-in HEAD.

## Cleanup

- Test-owned objects removed:
- Objects intentionally preserved because ownership was not provable:
- User windows/documents affected: none / describe exact failure

## Verdict

- Overall release: PASS / FAIL_CLOSED / POSTCONDITION_FAILED / FAIL / BLOCKED_REAL_E2E / UNKNOWN_OUTCOME
- Experimental PID pointer: NOT_SUPPORTED / NOT_RUN / EXPERIMENTAL_DIAGNOSTIC_ONLY / UNKNOWN_OUTCOME / FAIL
- Passed release gates:
- Failed exact cells and reasons:
- Not-run cells and reasons:
- Explicit limitations:
````

## 13. 禁止的结论

不得写：

- “Computer Use 已支持所有 macOS 应用”；
- “VS Code/Edge 被安全拒绝，所以兼容 PASS”；
- “WPS 以前生成过文件，所以本轮 PASS”；
- “Fixture 一个 click PASS，所以全应用 click PASS”；
- “实验性五场景产生了记录，所以 PID pointer 是 release PASS”；
- “测试注入 registry cell 是受支持的生产兼容结果”；
- “测试 skip/权限缺失等于通过”；
- “可以自动把实验或候选 cell 写入正式 registry”。

正确结论必须逐 bundle ID、精确版本、精确 action 独立陈述，并保留所有 `NOT_RUN`、`BLOCKED_REAL_E2E`、`NOT_SUPPORTED`、`EXPERIMENTAL_DIAGNOSTIC_ONLY`、`FAIL_CLOSED`、`POSTCONDITION_FAILED` 和 `UNKNOWN_OUTCOME`。ScreenCaptureKit 间歇 discovery 限制必须继续列为独立警告。
