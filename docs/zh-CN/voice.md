# 语音朗读

Astra 可以把回复念出来。朗读只是展示功能：它监听流式输出的回复，不会进入对话、
已保存的会话或发给模型的内容。只读回复正文，思考、工具输出、代码块、表格、链接
和表情都会跳过。

音频来自任何实现了 OpenAI 兼容 `POST {base_url}/audio/speech` 且能返回原始 PCM
的服务，云端语音 API 和本地模型服务都可以，所以由你决定下载开源模型还是接入
API。Astra 本身不附带语音模型。

## 配置

先安装音频播放依赖，然后重启 Astra：

```sh
astra setup --extra voice
```

在 Astra 主目录的 `settings.json`（源码安装是 `.astra/settings.json`）里加一个
`voice` 段。云端 API 只需要地址、密钥和音色：

```json
{
  "voice": {
    "enabled": true,
    "base_url": "https://api.example.com/v1",
    "api_key_env": "SPEECH_API_KEY",
    "model": "speech-model",
    "selected": "alloy",
    "voices": {"alloy": {"voice": "alloy"}, "nova": {"voice": "nova"}}
  }
}
```

想复用 `/connect` 保存过的服务商，把 `base_url` 和 `api_key_env` 换成
`"connection": "openrouter"`。

本地服务用的是同一个请求，只是地址换成本机。标准请求里没有的字段（例如克隆音色
的参考音频）写在 `request` 里。配置了 `server.command` 后，Astra 在第一次需要
朗读时启动服务，所有会话超过 `idle_seconds` 没有朗读、或启动它的会话退出时，
自动把它停掉：

```json
{
  "voice": {
    "enabled": true,
    "base_url": "http://127.0.0.1:18700/v1",
    "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
    "request": {"stream": true, "streaming_interval": 1.0, "lang_code": "auto"},
    "first_request": {"streaming_interval": 0.4},
    "selected": "温暖",
    "voices": {
      "温暖": {"request": {"ref_audio": "/path/to/warm.wav", "ref_text": "warm.wav 里说的原话。"}},
      "沉稳": {"request": {"ref_audio": "/path/to/calm.wav", "ref_text": "calm.wav 里说的原话。"}}
    },
    "server": {
      "command": ["/path/to/venv/bin/python", "-m", "mlx_audio.server", "--host", "127.0.0.1", "--port", "18700"],
      "idle_seconds": 600
    }
  }
}
```

这个例子用的是 Apple Silicon 上的 [mlx-audio](https://github.com/Blaizzy/mlx-audio)
（在单独的环境里 `pip install "mlx-audio[server]"`）。其他提供同一接口的服务都能用，
区别只在 `request` 里的字段。

| 字段 | 含义 |
|---|---|
| `enabled` | 是否朗读回复，`/voice on`、`/voice off` 会改它 |
| `selected` | 当前音色，`/voice use <名字>` 会改它 |
| `base_url` | 接口地址，后面会接上 `/audio/speech` |
| `connection` | 复用某个已保存服务商连接的地址和密钥 |
| `api_key_env` | 存放密钥的环境变量名 |
| `model` | 作为 `model` 字段发送 |
| `sample_rate` | 服务返回的 PCM 采样率，默认 24000 |
| `request` | 每次请求都附带的额外字段 |
| `first_request` | 一条回复的第一句用它覆盖 `request` 里的同名字段 |
| `voices` | 命名音色：服务商的 `voice` 编号、额外的 `request` 字段，或两者都有 |
| `server.command` | 按需启动本地服务的命令 |
| `server.idle_seconds` | 多久没有朗读就停掉本地服务，默认 600 |
| `server.startup_seconds` | 等待本地服务就绪的时间，默认 90 |
| `max_chars` | 一条回复最多朗读的字数，默认 600 |
| `prebuffer_seconds` | 每句开始前先缓冲的音频时长，默认 0.25 |

## 使用

```text
/voice                 查看是否开启和当前音色
/voice on | off        开启或关闭朗读
/voice use <名字>      切换音色，输入能唯一确定的前缀即可
/voice list            列出已配置的音色
/voice stop            立刻停止朗读
/voice test [文字]     用当前音色念一句试听
```

`/voice` 在所有模式下、回复进行中都可以用。发新消息、取消、切换会话时朗读也会
停止。终端里第一次按 Ctrl+C 会先让还在朗读的回复安静下来。

桌面端有对应的“语音朗读”面板，三个入口：输入 `/voice`，设置里的“语音朗读”，
或输入框旁的朗读按钮。这个按钮在配置了语音服务之后出现，显示朗读是否开启和当前
音色。面板里可以开关朗读、选择音色、试听。朗读进行时，输入框上方会出现“停止
朗读”，也可以按 Cmd/Ctrl+.；某条回复没能读出来时，同一位置会显示原因。

一条回复从第一个分句就开始念，之后一句一句来。引擎比说话慢时，Astra 会在句子
开始前多等一下，而不是在句子中间卡顿。`first_request` 可以让流式服务用小片段
快速起头，其余句子用更大、更省算力的片段。

## 限制

- 声音在运行 Astra 后端的那台机器上播放。
- 服务必须能在 `response_format: "pcm"` 时返回 16 位单声道 PCM。
- 重新生成的回复和唤醒摘要不会朗读。
- 每个打开的会话各念各的回复，两个会话可能同时出声。
- 本地语音模型的服务运行时可能占用数 GB 内存。
