# Astra ✦

**English** · [简体中文](README.zh-CN.md)

**A personal AI agent for everyday tasks, with computer use, memory and a terminal interface.**

Astra connects to model APIs to help you research, work with files and operate
applications. It keeps conversations and state on your machine, and also supports
optional local model endpoints.

[Quick start](#quick-start) · [ChatGPT login](#chatgpt--codex) · [Desktop preview](docs/gui.md) · [Everyday commands](#everyday-commands) · [Updates](#updates-and-data) · [Documentation](#documentation)

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

### Optional desktop preview

The source checkout also includes an Electron desktop preview. It needs **Node.js 22.12+**:

```text
astra setup --gui
astra --gui
```

To install the desktop and register the global command together, run
`./astra.sh setup --gui --install-command` from the checkout on macOS/Linux,
or `.\astra.bat setup --gui --install-command` on Windows. Then open a new terminal
and run `astra --gui` from your project directory. Plain `astra` opens the terminal UI.
Exit running Astra sessions before setup so their dependency environment can be updated.
GUI dependencies are downloaded only after enabling the component. The preview
provides conversations, model login, background sessions, approvals and file diffs;
full TUI parity and cross-platform native acceptance are still in progress.
See the [desktop guide and current limits](docs/gui.md).

### 2. Connect a model

Choose a ChatGPT account connection, an API provider, or a local model endpoint.

#### ChatGPT / Codex

Use your ChatGPT account to access its available Codex models. This connection
does not require an API key or changes to `.env`.

1. Open a **new terminal** (Windows CMD / PowerShell, macOS or Linux) and run:

   ```text
   astra auth login
   ```

   If Windows cannot find `astra` yet, run `.\astra.bat auth login` from the installation directory.
2. Open the URL shown in the terminal, enter the one-time code, and approve the login.
3. Wait until the terminal reports `Signed in.` and the number of available Codex models, then run `astra`.
4. **Inside Astra**, enter `/model`, choose **ChatGPT / Codex**, and select an available GPT model. This saves your startup model.

Already inside Astra? Enter `/connect`, select **ChatGPT / Codex → Subscription**,
and follow the displayed login instructions. Then choose a model in `/model`.

If OpenAI says device-code authorization is disabled, enable it in your ChatGPT
security settings, cancel the old attempt, and start login again.
See [OpenAI's device-code requirements](https://learn.chatgpt.com/docs/auth#preferred-device-code-authentication-beta).

**Connect each computer separately.** `astra update` preserves the current
installation's login, but does not sign you in or sync another computer's login.
The ChatGPT / Codex models appear in `/model` after you connect the account.

**No GPT models after updating?** Run `astra auth status` in your terminal.
If it reports `sign-in required`, run `astra auth login`. If it reports
`signed in`, restart Astra and use `/connect → ChatGPT / Codex` to refresh and
save the connection, then open `/model` again.

Astra displays reasoning summaries when the model returns them.
See [login details, reasoning summaries and troubleshooting](docs/codex-oauth.md).

#### API providers and local models

Edit `.env` in the installation directory. For example, to use the bundled
DeepSeek profile, set these existing entries with your own API key:

```dotenv
DEEPSEEK_API_KEY=your-api-key
LLM_MODEL=deepseek-flash
LLM_BASE_URL=https://api.deepseek.com
```

Other providers and local endpoints are supported through
[model profiles and `/connect`](docs/usage.md#model-connections).

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
| `/connect` | Connect a ChatGPT account, API provider or local endpoint. |
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
| Sign in with ChatGPT and select a GPT model | [Quick start](#chatgpt--codex) · [Codex login guide](docs/codex-oauth.md) |
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
