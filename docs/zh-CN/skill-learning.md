# 技能学习与手动检查

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../skill-learning.md)

[使用技能库](#use-the-library) · [技能归属](#skill-ownership) · [分批检查](#review-a-batch) · [迁移旧技能库](#migrate-an-older-library) · [维护参考](#maintenance-references)

Astra 可在工作中直接把可复用的方法保存为技能，无需每条经验都经过候选、实验和激活流程。由模型判断是否值得保存，并不要求每轮对话都产生技能。

<a id="use-the-library"></a>

## 使用技能库

```text
/skills
/skills show <name> [file]
/skills create [name] [description]
/skills create --template <name> <description>
```

`/skills create` 先通过对话起草完整技能，再由用户决定保存；`--template` 保留原来的空模板操作。

技能包含 `SKILL.md`，可附带 `references/`、`templates/`、`scripts/` 或 `assets/`。模型自己的总结使用 `skill_manage(origin="auto")`；用户要求加入的内容使用 `origin="user"`。提示中只注入目录，完整指令按需读取。内置规则有单独的[加载约定（英文）](../runtime/core-rules.md)。

<a id="skill-ownership"></a>

## 技能归属

| 来源 | 含义 | `/learn review` 是否检查 |
| --- | --- | --- |
| `auto` | 模型总结并维护的方法 | 是 |
| `user` | 用户要求加入、安装或编辑的技能 | 否 |
| `builtin` | 随 Astra 打包的核心规则 | 否 |

自动技能通常位于 `.astra/skills/learned/<name>/`；新用户技能位于 `user/`，已有分类保持原路径。`AGENT_SKILLS_PATH` 可指定其他技能库。归属以写入方的来源记录为准，不能靠正文声明或目录名称伪造。无法确认归属的内容按用户技能保护，`/skills` 显示归属和分类。

技能应说明何时使用、步骤、限制和来源。它会进入普通技能目录，相关任务通过 `skill_view` 读取。一次记录的经验不保证命令在其他机器或版本仍能运行。

<a id="review-a-batch"></a>

## 分批检查

```text
/learn
/learn review
/learn history
/learn history <run-id>
/learn undo <run-id>
```

`/learn review [技能名]` 进入普通对话，使用当前模型和思考强度。首轮读取自动技能及其来源，提出具体修改与必要的验证方案，然后等待用户选择。首轮由运行时限制为只读，不能保存修改或执行技能步骤；用户技能仍受保护。

你可以追问原因、纠正判断，或回复“只改第二条”“继续”。后续按你选定的范围修改和验证；“继续”覆盖刚才明确展示的方案，范围内不重复确认。内容审查、保存修改、实际执行和验证通过会分别说明。

每份快照最多包含 4 个技能、24,000 字符的正文和来源。命令可进行普通工具调用，不再使用原来的独立 90 秒、4,096 token 审查请求。成功应用一批决定后，共享游标才前进；超大或受保护的条目会报告未检查。指定技能名的审查不移动分批游标。

讨论期间不持有技能库锁。快照保存在当前进程内并绑定会话；重启后需要重新读取、核对先前建议。文件已变化、操作无效或遗漏批内技能时，拒绝应用。原版本与原因仍保存在 `.astra/skills-learning/`，可通过 `/learn history` 查看、`/learn undo` 撤销；撤销不会覆盖后续修改。没有后台审查。

`Ctrl+C` 取消当前对话回合。生成期间的新消息会引导同一回合，不会解除首轮只读限制；应在建议输出完成后，用下一条消息决定执行范围。已经提交的修改仍保留在历史中。

`/learn mode off` 关闭新自动总结的直接保存；`/learn mode review` 重新开启。这里的旧名称 `review` 不代表定时维护。即使关闭自动保存，仍可明确执行 `/learn review`。

<a id="migrate-an-older-library"></a>

## 迁移旧技能库

```text
/learn migrate
```

迁移会备份旧 SQLite 库，使用现有文件事务和撤销机制导入符合条件的自动技能总结。记录原 ID 和目标位置，重复执行不会重复导入。名称冲突和旧补丁不能覆盖用户技能，原数据库记录继续作为历史保留。旧候选队列本身（审核器、试用和 `/learn legacy` 命令）已退役；`/learn` 会显示还有多少候选未迁移。

观察和环境笔记保留在历史库，不转成技能，可通过[本地历史检索](local-history-retrieval.md)读取。因此旧候选数量、自动技能数量和本批检查数量可能不同。

<a id="maintenance-references"></a>

## 维护参考

实现位于 `agent/runtime/skill_learning.py`、`skill_curation.py`、`skill_provenance.py`、`skill_migration.py` 和 `agent/cli/learning_commands.py`。测试覆盖归属、游标、无效输出、并发修改、迁移和撤销。可选 `scripts/skill_curation_acceptance.py` 使用历史数据副本做验收；原始记录和模型输出不要提交到 Git。
