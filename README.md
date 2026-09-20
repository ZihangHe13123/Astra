# Astra ✦

**English** · [简体中文](README.zh-CN.md)

**A personal AI agent for everyday tasks, with computer use, memory and a terminal interface.**

Astra connects to model APIs to help you research, work with files and operate
applications. It keeps conversations and state on your machine, and also supports
optional local model endpoints.

[Quick start](#quick-start) · [Everyday commands](#everyday-commands) · [Updates](#updates-and-data) · [Documentation](#documentation)

## What you can do

| Capability | How it helps |
| --- | --- |
| **Computer use** | Work with browser pages and selected macOS application windows. See the [browser](docs/browser-interaction.md) and [Mac](docs/macos-computer-use.md) guides. |
| **Bring a window into chat** | [Appshot](docs/appshot.md) adds a screenshot and available interface text to your draft on macOS and Windows. Add a question, then send it. |
| **Research and everyday work** | Search the web, read and edit files, run code and execute notebooks with configurable tool permissions. |
| **Memory and skills** | Keep preferences, retrieve useful history and save reusable methods. Manually review automatically learned skills without changing user-added ones. |
| **Continue your work** | Inspect task history, cancel work and resume eligible tasks with saved checkpoints. |
| **Choose your tools** | Switch model profiles, connect local endpoints or add optional MCP servers, QQ messaging and other integrations. |

Lyra is Astra's default persona. For a small terminal easter egg, try `/bar`.

## Quick start

**Pre-release:** install from a Git checkout. A complete standalone installer is
not available yet; the Python wheel alone does not include the full terminal UI
and native helpers.

### 1. Install

You need **Python 3.11+**, **Node.js 18+ with npm**, and **Git**.

**Windows — CMD or PowerShell**

```powershell
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
.\astra.bat setup --install-command
```

**macOS or Linux**

```bash
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
./astra.sh setup --install-command
```

Setup prepares the Python environment and terminal UI, registers `astra` in your
user PATH, and creates `.env` if needed. Existing configuration is preserved.

### 2. Connect a model

Edit `.env` in the installation directory. For example, to use the bundled
DeepSeek profile, set these existing entries with your own API key:

```dotenv
DEEPSEEK_API_KEY=your-api-key
LLM_MODEL=deepseek-flash
LLM_BASE_URL=https://api.deepseek.com
```

Other providers and local endpoints are supported through
[model profiles and `/connect`](docs/usage.md#model-connections).

To try a ChatGPT subscription, run `astra auth login` or choose
**ChatGPT / Codex** in `/connect`, then select its model in `/model`.
See [Codex login and reasoning summaries](docs/codex-oauth.md).

### 3. Start

Open a **new terminal**, change to the folder you want to work in, then run:

```text
astra doctor
astra
```

Inside Astra, use `/connect` to add a provider, `/model` to browse its models, and `/help` to explore commands.
The current folder is the workspace unless `SANDBOX_WORKDIR` overrides it.

Already have an older installation? See the
[upgrade guide](docs/launcher-update.md#upgrade-an-older-checkout).

## Everyday commands

Enter these **inside Astra**:

| Command | Purpose |
| --- | --- |
| `/help` · `/doctor` | Find commands and check runtime connections. |
| `/model` · `/mode high` | Select a model and adjust supported reasoning effort. |
| `/memory` · `/skills` | Inspect memory and the skill library. |
| `/learn review` | Discuss improvements to automatic skills, then choose edits and verification. |
| `/tasks` · `/resume <task-id>` | Inspect task history or resume an eligible task. |
| `/restart` · `/restart cancel` | Restart this backend after the current work finishes, or cancel the wait. |
| `/wakeup` · `/wakeup cancel` | Inspect or stop a [session wakeup](docs/session-lifecycle.md). |
| `/appshot enable` | Enable window capture after installing the native helper. |

`Ctrl+L` opens activity details, `Ctrl+O` opens the latest tool result, and
`Ctrl+C` requests cancellation. See [everyday use](docs/usage.md) for details.

## Updates and data

Run these **in your terminal**. Close interactive Astra sessions before applying
an update; recognized companion services are paused and restored automatically.

```text
astra update --check
astra update
```

Start `astra` again to use the new version. Updates follow this checkout's Git
upstream. Source installations keep configuration in `.env`, private state in
`.astra/` and conversations in `.sessions/`; these are preserved during updates.
See [local changes, backups and recovery](docs/launcher-update.md).

Local storage does not mean local inference: material included in a request is
sent to the selected model provider. Computer use needs the relevant helper,
model capabilities and OS permissions. General native Windows computer control
is not yet provided by its Appshot helper; see [platform limits](docs/appshot.md).

## Documentation

| I want to… | Read |
| --- | --- |
| Install, update or recover | [Launcher guide](docs/launcher-update.md) |
| Configure models and use the terminal | [Everyday use](docs/usage.md) |
| Use a browser or desktop application | [Browser interaction](docs/browser-interaction.md) · [macOS Computer Use](docs/macos-computer-use.md) · [Appshot](docs/appshot.md) |
| Understand memory and learning | [Memory overview](docs/memory.md) · [Skill review](docs/skill-learning.md) |
| Add search, MCP, messaging or image generation | [Integrations](docs/integrations.md) |
| Configure sandboxing and file access | [Tool execution](docs/execution.md) |
| Develop, test or maintain Astra | [Development guide](docs/development.md) |

For architecture, activity history, notebooks and troubleshooting, browse the
[full documentation index](docs/README.md).

## License

Astra is licensed under the [MIT License](LICENSE).
Third-party dependencies and evaluation materials retain their own licenses;
see the [evaluation sources](evals/coding/README.md#sources-and-licenses).
