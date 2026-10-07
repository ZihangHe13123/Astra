# Voice output

Astra can read its replies aloud. Speech is a presentation feature: it listens to
the reply as it streams and never becomes part of the conversation, the saved
session or what the model is sent. Only the reply text is spoken. Reasoning, tool
output, code blocks, tables, links and emoji are skipped.

Audio is produced by any server that implements the OpenAI-compatible
`POST {base_url}/audio/speech` request and can answer with raw PCM. That covers
hosted speech APIs and local model servers alike, so you choose whether to
download an open model or connect an API. Astra ships no speech model.

## Set up

Install audio playback once, then restart Astra:

```sh
astra setup --extra voice
```

Add a `voice` section to `settings.json` in your Astra home (`.astra/settings.json`
in a source checkout). A hosted API needs only an endpoint, a key and a voice:

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

Use `"connection": "openrouter"` instead of `base_url` and `api_key_env` to reuse
the endpoint and key of a provider saved with `/connect`.

A local server is the same request sent to a loopback address. Fields the
standard request does not have, such as the reference audio of a cloned voice,
go in `request`. With `server.command`, Astra starts the server when speech is
first needed and stops it when no session has spoken for `idle_seconds`, or when
the session that started it ends:

```json
{
  "voice": {
    "enabled": true,
    "base_url": "http://127.0.0.1:18700/v1",
    "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
    "request": {"stream": true, "streaming_interval": 1.0, "lang_code": "auto"},
    "first_request": {"streaming_interval": 0.4},
    "selected": "warm",
    "voices": {
      "warm": {"request": {"ref_audio": "/path/to/warm.wav", "ref_text": "The words spoken in warm.wav."}},
      "calm": {"request": {"ref_audio": "/path/to/calm.wav", "ref_text": "The words spoken in calm.wav."}}
    },
    "server": {
      "command": ["/path/to/venv/bin/python", "-m", "mlx_audio.server", "--host", "127.0.0.1", "--port", "18700"],
      "idle_seconds": 600
    }
  }
}
```

This example uses [mlx-audio](https://github.com/Blaizzy/mlx-audio) on Apple
Silicon (`pip install "mlx-audio[server]"` in an environment of its own). Any
server with the same endpoint works; only the `request` fields differ.

| Field | Meaning |
|---|---|
| `enabled` | Read replies aloud. `/voice on` and `/voice off` change it. |
| `selected` | The voice in use. `/voice use <name>` changes it. |
| `base_url` | Endpoint base; `/audio/speech` is appended. |
| `connection` | A saved provider connection whose endpoint and key to reuse. |
| `api_key_env` | Name of the environment variable that holds the key. |
| `model` | Sent as `model`. |
| `sample_rate` | Sample rate of the PCM the server returns. Default 24000. |
| `request` | Extra fields sent with every request. |
| `first_request` | Fields that replace `request` values for a reply's first sentence. |
| `voices` | Named voices: the provider's `voice` id, extra `request` fields, or both. |
| `server.command` | Command that starts a local server on demand. |
| `server.idle_seconds` | Stop the local server after this long without speech. Default 600. |
| `server.startup_seconds` | How long to wait for the local server to answer. Default 90. |
| `max_chars` | Read at most this many characters of one reply. Default 600. |
| `prebuffer_seconds` | Audio buffered before a sentence starts. Default 0.25. |

## Use

```text
/voice                 show whether voice is on and which voice is selected
/voice on | off        read replies aloud, or stay silent
/voice use <name>      switch voice; a unique prefix is enough
/voice list            list the configured voices
/voice stop            stop speaking now
/voice test [text]     speak a sample with the selected voice
```

`/voice` works in every mode and while a reply is running. Speech also stops
when you send a message, cancel, or change session. In the terminal the first
Ctrl+C silences a reply that is still being spoken; in the desktop app use the
stop button shown while speaking, or Cmd/Ctrl+.

A reply starts being spoken at its first clause, then one sentence at a time.
When the engine is slower than speech, Astra waits before a sentence rather than
stuttering inside it. `first_request` lets a streaming server start the first
sentence with small pieces and produce the rest in larger, cheaper ones.

## Limits

- Audio plays on the machine that runs Astra's backend.
- The server must return 16-bit mono PCM for `response_format: "pcm"`.
- Regenerated replies and wake-up summaries are not read.
- Each open session speaks its own replies; two sessions can talk at once.
- A local speech model can hold several gigabytes while its server runs.
