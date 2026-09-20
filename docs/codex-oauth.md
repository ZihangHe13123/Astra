# ChatGPT / Codex subscription login

Astra can use a ChatGPT account through a native `openai-codex` provider.
Requests go directly to OpenAI's Codex Responses endpoint. Astra still runs its
own tools, agent loop, memory and Team workers. No subscription-to-API proxy or
separate Codex agent process is involved.

## Sign in

In **ChatGPT → Settings → Account security & login** (formerly **Security**),
scroll to the bottom and enable **Codex device-code authorization** if it is
disabled. In Chinese, the section is **账户安全与登录** and the switch is
**为 Codex 启用设备代码授权**. A disabled Continue button on the OpenAI consent
page means this account setting needs attention.

From a terminal:

```text
astra auth login
```

Open the displayed `https://auth.openai.com/codex/device` address, enter the
one-time code and approve the login. Then start Astra and choose
**ChatGPT / Codex** in `/model`. The model list comes from your account's catalog.
Signing in saves the connection; selecting a model saves your startup default.

Alternatively, use `/connect` inside Astra and select **ChatGPT / Codex →
Subscription**. The panel shows the URL and code while waiting. Esc cancels the
connection attempt. If you change the device-code authorization setting during
login, cancel the old attempt and start a new one.

```text
astra auth status
astra auth logout
```

`status` reports whether Astra has saved credentials, without printing tokens.
`logout` removes Astra's local credentials. A saved model selection remains in
place and prompts for sign-in on its next request.

## Reasoning and tools

Returned reasoning summaries appear in the existing thinking display with the
label **推理摘要**. Use Astra's existing thinking visibility control to show or
hide them. A model may provide no summary; Astra does not fabricate one or claim
to display private reasoning.

`/mode low`, `/mode high` and `/mode max` set supported reasoning effort.
`max` uses the highest effort advertised for the selected model that the Codex
endpoint accepts. An unsupported explicit effort reports an error rather than
silently substituting another.
Ordinary Chat Completions sampling/output-limit parameters are not forwarded:
the subscription endpoint does not support those parameters.

Encrypted reasoning continuation and assistant message phases are saved with
the session. They are sent back only to the same account and model. They are
kept out of visible messages and other providers' requests. Editing or
compacting message content invalidates the affected continuation state. Tools
are executed only after a complete, validated response; interrupted or
contradictory calls are discarded.

## Storage and connection errors

Credentials live in Astra's private state directory at `auth/codex.json`
(`.astra/auth/codex.json` for a source checkout, or under `ASTRA_HOME`).
Writes are atomic and owner-only on POSIX. Refreshes are serialized across
Astra processes. Astra never reads or modifies Hermes or Codex credential files.
Keep this private state out of Git, along with other Astra session data.

Account availability and subscription usage limits apply. There is no automatic
fallback to a paid API key. A rejected/revoked refresh token requires a new
`astra auth login`; an expired device code requires restarting login.
Authentication and inference use fixed OpenAI origins; this provider cannot be
redirected to a custom endpoint.

Signing in and listing models confirm authentication and discovery. A successful
conversation is needed to confirm inference access for your selected model.

References: [Codex authentication](https://learn.chatgpt.com/docs/auth) and
[reasoning summaries and encrypted continuation](https://developers.openai.com/api/docs/guides/reasoning).
