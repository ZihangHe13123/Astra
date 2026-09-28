# Notebook 执行

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../notebook-execution.md)

[范围与行为](#scope-and-behavior) · [用法](#usage) · [验证](#validation)

<a id="scope-and-behavior"></a>

## 范围与行为

`notebook_execute` 默认不在工具清单中。设置 `AGENT_TOOL_EXPOSE=notebook_execute` 后模型才能使用它；否则 Notebook 通过 `execute_shell` 在内核自己的环境中运行。

`notebook_execute` 复用 Python 执行审批、所选 Local/Docker 沙箱和后台进程管理。单元编号从 1 开始，包含 Markdown 单元，区间两端均包含；支持跳过单元、逐单元超时和保存输出，不自动安装依赖。

每次调用使用新内核，不自动补跑前面的初始化单元；有依赖时从第 1 个单元选起。`kernel_name` 指已安装 Jupyter kernelspec 名称，不是 Python 路径。工作目录为源 Notebook 所在目录。

输出必须是新的 `.ipynb`，默认带随机后缀。目标已存在或与源相同时，在启动内核前拒绝。输出副本清除旧输出（包括未选单元），在 `metadata.astra_execution` 记录选择和状态。运行器不修改源字节，但 Notebook 中的代码仍拥有执行环境授予的正常权限。

每个单元结束时原子保存副本，并在进程日志输出 JSON 进度。错误/超时停止后续执行，保留部分输出。强制取消可能让当前单元来不及保存，但最近一次已保存边界仍在。不承诺持久内核恢复；正常关闭和超时通过 nbclient 管理。

<a id="usage"></a>

## 用法

在实际执行环境中安装可选扩展：

```sh
pip install -e '.[notebook]'
```

工具参数示例：

```json
{"path":"work.ipynb","end_cell":10,"skip_cells":[7],"cell_timeout":600,"background":true}
```

返回的 `process_id` 可传给 `process_poll`、`process_read`、`process_cancel`。Notebook 标准输出与图片位于结果 Notebook，进程日志只含单元进度及执行诊断。

<a id="validation"></a>

## 验证

本地真实内核测试覆盖区间/跳过、单次执行中的共享变量、源文件保留、失败输出和超时保存；注册表 → 后台管理器 → 内核路径验证轮询、日志和结果。零/非零退出测试保证普通 stderr 诊断不会把成功进程判成失败。

Docker 转发复用既有 Python 沙箱路径，不代表已对当前 Docker 或具体课程 Notebook 做过新验收。执行基础见 [nbclient 官方文档（英文）](https://nbclient.readthedocs.io/en/latest/client.html)。
