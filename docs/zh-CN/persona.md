# 公开版 Lyra 人格

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../persona.md)

[本地私人定制](#private-local-customization) · [人格定义与会话状态](#definition-and-session-state) · [酒吧彩蛋](#bar-easter-egg) · [可选的本地模式](#optional-local-mode) · [验证范围](#validation)

Astra 内置一个面向工作和日常交流的公开人格 `lyra`。Lyra 是温和、直接的 AI 协作者：跟随用户语言、依据证据、完成已授权工作并说明不确定性。默认设定不会给用户安排姓名、身世或私人关系，也不会虚构共同经历。

用 `/persona` 查看可用配置，用 `/persona lyra` 选择。已保存的选择优先于 `AGENT_PERSONA`，两者都没有时使用默认 `lyra`。`/mode` 单独控制推理强度，`/think` 控制推理显示。公开人格把 temperature 留给服务方配置，除非当前运行模式有自己的采样设置。

<a id="private-local-customization"></a>

## 本地私人定制

可在 Astra 私有状态目录放置 `persona.local.json`，提供本地人格或创作模式提示词。源码安装默认路径为 `.astra/persona.local.json`；`ASTRA_HOME` 可修改状态目录，`ASTRA_PERSONA_FILE` 可指定文件。Astra 不会在无关工作项目中自动发现此文件。它应保存在 Git 和公开产物之外。

```json
{
  "schema": 1,
  "profiles": [
    {
      "name": "personal-lyra",
      "description": "My local collaboration style",
      "persona": "Use concise replies and explain important tradeoffs.",
      "version": 1,
      "persona_layer": "style"
    }
  ]
}
```

可选 `mode_prompts` 对象接受 `bar_atomic`、`bar_stream` 的完整提示词，只替换对应文本；控制器继续约束会话隔离和工具访问。酒吧覆盖提示词必须保留对应的场景工具契约。配置无效时报告错误，不会悄悄切换人格并覆盖已保存设置。

显式本地配置优先于同名的公开版迁移别名，使私人安装在更新公共代码时保留自己的配置与会话状态。没有该文件的新安装只获得公开 Lyra。修改提示词不会自动刷新已运行的会话，需要重新选择人格或重新打开模式。

<a id="definition-and-session-state"></a>

## 人格定义与会话状态

人格框架将带版本的稳定定义与会话状态分开。状态可记录该会话的工作模式和上下文，内置定义不包含私人关系基线。人格风格不改变工具权限、事实标准或完成任务的判定；[核心规则（英文）](../runtime/core-rules.md) 是独立的一部分。

已退役的预设 ID 若没有显式本地配置，会解析为公开 Lyra。加载对应会话时重建系统提示词，清理旧预设的模式、关系和情绪状态，丢弃缓存的系统投影。精确指纹还能识别引入版本号前的内置提示词，而无需继续打包旧文案。下次保存会话时写入规范化结果，对话消息保留。任意自定义提示词不会通过宽泛文本匹配被识别或重写。

这些兼容处理不等于隐私导出：不会清理对话消息、私人记忆、自定义提示词或 Git 历史。准备公开源码时，仍应让这些内容保留为私有数据。

<a id="bar-easter-egg"></a>

## 酒吧彩蛋

`/bar` 打开适合一般受众的原创虚构咖啡馆/酒吧场景。它使用独立会话命名空间，不读取工作会话记忆。提示词区分虚构情境与用户的真实身份、经历；场景工具只影响本地可视化。用 `/bar leave` 返回普通工作。

<a id="optional-local-mode"></a>

## 可选的本地模式

安装可通过 `.astra/local_mode.py` 或显式 `ASTRA_LOCAL_MODE_FILE` 保留私人模式，默认不打包、不启用。它是受信任的本地 Python 代码，仅在后端启动时加载一次，不从工作项目发现。保持在 Git 之外；模块无效时返回配置错误，不静默改变行为。

模块导出 `API_VERSION = 1` 和 `create_mode(agent)`，返回 `agent.runtime.local_mode` 的 `LocalMode`。需要提供控制器、唯一斜杠命令、名称、描述、私人会话命名空间和状态模板，可选提供界面标题、状态及输入提示。

控制器遵守同模块的 `ModeController` 契约：enter/switch/leave 和 undo/retry。它必须保存并恢复工作上下文，在生命周期回调前设置 `agent.local_mode`，禁用工具以及所有记忆/任务/上下文提供方，并在失败时保留会话数据。宿主提供命令路由、取消、独立会话发现和阻止向工作记忆/任务导出的保护。

替换了智能体设置的控制器，还可以通过可选的 `work_settings()` 按属性返回工作智能体原本的值（包括 `yolo`）。模式打开期间，借用智能体的微信或 QQ 回合会用这些值运行，与 Bar、Minimal 相同；没有这个方法时，这类回合沿用模式自己的设置。

源码更新保留被忽略的本地模块。控制器代码仍由本地维护并保持接口兼容，重启后加载变化。`persona.local.json` 中私有模式的提示词键作为不解析的本地数据保留。

<a id="validation"></a>

## 验证范围

人格回归测试覆盖保存的选择、状态恢复、缓存提示词投影、采样优先级和回放/压缩。回放使用合成对话检查运行时契约，不衡量真实模型质量。`scripts/persona_ab.py` 可用已配置模型比较两种提示词布局，使用固定合成记忆样本，不读取私人记忆库；真实运行仍会将样本发送给选定服务方。
