---
name: using-computer-use
description: Use when controlling macOS interfaces with computer_* tools.
---

# Using Computer Use

## Overview

以最新目录与快照为准。

常规填写直接 apps → get_app_state → 批量 act。已有题目/字段和用户目标时，不先查旧 session、旧失败总结或执行 shell；确实缺少答案资料时再定向读取。优先按当前 computer_receipt.next_step 行动，旧诊断不能推翻本次已验证状态。

## When to Use

用户指定原生 CU 时使用本 skill，包括浏览器里的网页；不要自行换通道。未指定通道的网页任务可使用 browser tools，并读取 `visible-browser-cdp`。

## Quick Reference

1. Read: `computer_apps` → `computer_get_app_state` with exact refs from
   the latest catalog; it does not focus the app or move the real pointer.
   `computer_focus` is explicit advanced; `computer_snapshot` refreshes the bound target.
   `/computer status` is cached and nonactivating; `computer_status` queries the helper and refreshes that cache.
   Neither status path prompts nor opens System Settings. 仅应用户要求运行 `/computer setup`。
   Choose only `bindable=true` catalog windows. `ax_window_unmatched` 表示窗口仍在但缺少唯一 AX 匹配，不代表引用过期或系统禁令。状态未变时停止重复绑定；面板状态改变后刷新目录获取新引用。Never reuse or implicitly rebind an old ref. 不把被遮挡父窗口当替代目标。
2. Target-window capture/element refs default. Display needs explicit once approval.
   Latest snapshot: one snapshot permits one act. 成功的 `computer_get_app_state`、
   `computer_act`、`computer_resume` 返回的新 snapshot_id 和 element refs 可用于下一步；
   target_window 范围、目标未变且没有已知失效时，不必额外 `computer_focus` 或 `computer_snapshot`。
   每次只用最新返回的一组，上一组不能再次使用。
3. Only in-memory or document-content editing without file/external effect is
   ordinary session + bundle work. Save As, Save a Copy, create or overwrite a local
   file, send, submit, export, upload, share, delete, install, System Settings, and
   Terminal Enter need high-impact exact-batch once approval. YOLO/autoapprove 自动授予本次权限，cannot downgrade 风险或目标校验。
4. Target or action invalidation or authority invalidation clears snapshot,
   element, plan, and exact approval. 普通前台应用授权跨观察保留，中断交接关闭或发布失败撤销。 Smart approval for `text_detail=on` is once-only/target-bound.
   Bounded errors fail closed: never guess a target/ref/artifact.
   `unsafe_artifact`: `computer_close` → fresh session →
   `computer_apps` → `computer_get_app_state`.
5. Password, secure field, OTP, Touch ID, authentication, payment, or IME:
   prohibited; handoff to the user. Never operate macOS permission/admin UI.
6. High-impact file create/read/overwrite/export approval target and copy include
   the trusted canonical user-selected filesystem destination. Non-file
   high-impact (Terminal Enter, send, submit, system settings) uses trusted app,
   window, action class, effect, and boundary; does not require a filesystem destination. Exclude
   typed text, secure values, internal refs, session IDs, request-local screenshot,
   cache paths, and helper paths.
7. Real-pointer actions need an exact bundle/version/action cell; evidence never
   transfers. Virtual-cursor plans restore cursor/prior app. Failed evidence stays
   disabled; never use global HID fallback.
   `docs/macos-computer-compatibility-evidence.md`.
8. After `unknown_outcome`, timeout, cancellation, or helper loss, never retry or replay
   an unacknowledged semantic action—even if a fresh snapshot looks unchanged.
   A fresh snapshot is diagnostic only. `last_ack` is the zero-based index of the
   last post-guard confirmed action; `-1` means none. Only actions `0..last_ack`
   are confirmed. Every action after `last_ack` has unknown semantic outcome and
   must not be automatically replayed. Handoff and ask the user to decide.
9. `click` 的 `checked` 目标返回 `choice_verification.status=verified`，表示这批目标在最终观察中全部匹配；已满足时不会重复点击。普通 `verified` 表示数值方向或 AXPress 控件变化。 `noop` is not completion:
   inspect a fresh snapshot and choose another explicit action.
   `unknown_outcome` forbids replay. Return/default-button requires fresh
   observation and an explicit new action.
10. Use `computer_handoff`. 挂起后使用回执中的恢复参数；用户交还控制后再恢复，不要先枚举目录。After `computer_resume`, resume with a fresh snapshot
    from its response; recapture only when stale.
11. Deadline cannot cancel an in-flight write, close an unknown session,
   or skip snapshot or approval. Between actions, stop safely; unknown outcome
   requires handoff. The user may perform the action personally; retry
   does not authorize the agent to replay the original semantic action. Call
   `computer_close` only when no write is in flight and no unknown outcome needs
   handoff; close cleanup clears state.

## 路由与首次操作

若下一步需要终端审批，在首次交互前提醒用户可以切回 Astra 终端批准；这是正常流程，不要求一直保持目标应用在前台。审批后的 Terminal 前台状态不能证明此前事件打到了 Terminal，也不能作为「事件未送达」或 key 状态的证据。由生产 takeover 流程处理焦点；只有工具报告目标/引用失效或出现已知界面变化时，才按下方恢复规则重新观察。

- 优先用 fresh AX `element_ref` 执行语义动作；截图坐标是窗口相对，且必须绑定实际目标。AX detail 文件的屏幕坐标不可直接作为 act 坐标。
- `computer_apps.routing_advice` 区分精确版本配置与 `generic_foreground`。未配置应用、`foreground_keyboard` 或 `requires_active=true` 路径可保留默认 `interaction_mode="auto"`，工具在输入前内部规划一次前台接管；显式 `foreground_takeover` 仍可用：通用前台不依赖版本登记，会使用真实鼠标；后台仍严格匹配。建议不是实机保证，helper plan、授权和用户活动检查始终生效。
- 原生/自定义按钮首次打开文件/保存/模态面板：必传 `opens_dialog=true` 或 `foreground_takeover`，在点击前接管；不补点已打开的面板。网页优先 `browser_upload`。
- 不自行 activate、固定 sleep、直连 helper 或改兼容表绕过焦点/交互失败。`config/macos_computer_compatibility.json` 的配置与 signed helper resource 要一致；应用升级后重新核对，不能套用历史成功结论。
- 先看 `form_controls.elements` 的 label/title、context、checked 和当前 ref/index；无需逐层展开深层网页。新版 helper 扩展网页分支深度。窗口栏挤占表单时，get_app_state 自动做一次同窗口原生子树读取。仍截断时用当前 ref 指定 subtree_ref；`subtree_v1` 表示这会重新读取原生子树，旧 helper 仍只做已捕获内容投影。

## 连续操作与恢复

1. 正常短流程：apps → get_app_state → act。十题等短时选择任务，先把题目与答案匹配好，再把独立选项放进同一个 actions（每项 click + checked:true）；工具逐项执行一次并核验，批末返回新快照。不要逐题调用 focus/snapshot/plan/begin_takeover，不把后台 plan_ref 传给 begin_takeover。需要继续时使用 act 返回的新 snapshot_id/ref。每一步先检查结果和目标效果，同一窗口串行，不提前排队后续输入。
2. 只有最新返回观察之后又发生页面/窗口/弹窗变化、用户操作、引用失效、需要更晚效果或缺少新观察时，才追加 `computer_snapshot`。切换应用或 helper 重启：重新 apps → get_app_state，不使用旧 ref。
3. 不为等待动画反复点击。computer_receipt 的 dispatch_state=acknowledged 表示动作已确认，verification_state=unverified 只表示业务结果未确认。next_step=observe_result 时调用 computer_snapshot(settle_ms=300) 后读结果；最多两次无新增证据的观察，然后报告未确认。effect_pending 表示工具阻止了同一待确认按钮的补点，也按此观察，不换 ref 重试。
4. 空 AX 树：先看当前目标截图。登录/权限界面交给用户；普通界面检查覆盖限制，最多做一次针对性刷新或已有 subtree 投影。依然没有可验证目标时报告缺少能力，不猜 element_ref，不自动走全局 HID/剪贴板/临时脚本兜底。
5. `stale_snapshot` 不一定代表未派发。只有返回证据明确该批次输入前被拒绝，才允许刷新后重新规划一次；`last_ack=-1` 单独不足以证明零输入。`unknown_outcome`、部分执行或效果不明遵守上方 no-replay 规则。
6. 截图、快照和 ref 都是 request-local。失效后的旧图片/锚点不再作为当前状态；截图能证明视觉变化，不能代替原生 `effect_verification` 或文件保存后的核验。

## 菜单、坐标与结果核验

Finder 翻页/关标签、WPS 保存/目录/输入法任务，再读取 [文件与桌面工作流](references/native-file-workflows.md)。普通网页表单无需加载这些案例。

- 语义按钮回执成功却没有效果，不连续重复 AXPress，也不默认改用未获支持的快捷键。正常授权下选择菜单等已观察到的新动作；未知结果仍遵守 no-replay。
- 坐标默认是窗口相对逻辑坐标；若依据本次返回图片选点，明确传 coordinate_space="image_pixels"，工具按 published_image_size 转换。不可使用旧 Appshot 的屏幕坐标。优先用 `target_element_ref` 绑定具体目标控件。前台视觉坐标可省略引用，由最新快照限定窗口；引用失效必须重新观察。没有效果时不凭标题栏高度猜偏移、读取旧 AX 缓存再点，或改用全局 Tab/Return 重试。`action_observation` 仅提供匹配控件的值变化或目标标签仍在的证据；`unchanged` 不授权重放，`unknown` 不等于未执行，树里没有标签也不是关闭证明。
- `post_action_observation_pending`／`next_step=follow_recovery`：动作已确认但没有返回新观察，后续界面未确认，不等于输入失败或保存成功。按本结果 Recovery 走（弹窗切换时即 `window_transition.next_observation`），不按 observe_result 截图；建议本身不授予权限，不重放原动作。

## Common Mistakes

- Reusing a snapshot after focus, window, modal, or app change.
- Inferring another app/version/action or bypassing approval/failures.

## 窗口内容不可读

`window_content_unavailable`：目标窗口全透明；停止视觉操作，不推断遮挡、失焦或点击失败，不反复截图、激活、移动或重放。交给用户检查应用捕获状态，改变后刷新目录重绑窗口。欢迎页切主界面可能换窗口身份，不沿用旧坐标；详见微信专用skill。

## 短时表单验收

使用当前 `click(element_index=当前索引, checked=true)`；label空时读title。`observation_identity` 只用于跨观察核验，不能操作。`choice_verification` 全部verified才代表选中，不代表提交。旧helper缺`checked_click_v1`时说明需更新，不重发字段。

## 表单推进与文本输入

- 首次切换到 CU 后尽早完成一个实际字段并读回，确认输入通道可用，再扩展到其余字段。
- 优先点击题号导航或已观察到的定位控件；图片数字首次读清后记录为结构化数据，只对仍不清楚的部分重新观察。不要反复宣称已确认又从头滚动。
- 快照失效不等于时间超期；按回执重新观察，不猜测原因。
- 长文本一次提交，由工具分块；不要先写剪贴板再尝试粘贴。未知结果时观察并交接，不自动补打未确认的部分。

`overlay_blocked`：遮挡未变停止重试。`no_suspended_target`：无目标可恢复。绑定同因失败两次即停止。`verified=false` 不得称逐字通过且空白等价与旧题图均须单列证据边界。
