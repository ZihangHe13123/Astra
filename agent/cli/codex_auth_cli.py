"""Terminal login without starting a model session or requiring the TUI."""
from __future__ import annotations

import asyncio
import sys

import httpx

from agent.runtime import codex_auth


async def run(action: str) -> int:
    if action == "logout":
        await codex_auth.logout()
        print("Signed out of ChatGPT in Astra.")
    elif action == "status":
        status = codex_auth.auth_status()
        print("ChatGPT: " + ("signed in" if status["connected"] else "sign-in required"))
        print("Astra credential file: " + status["path"])
    elif action == "login":
        async def progress(challenge):
            print("If authorization is disabled, enable Codex device-code login in ChatGPT Settings → Security.", flush=True)
            print(f"Open {challenge['verification_uri']}\nEnter code: {challenge['user_code']}\nWaiting for authorization…", flush=True)
        await codex_auth.device_login(progress)
        from .connections import connect_provider
        _, catalog = await connect_provider("codex")
        print(f"Signed in. {len(catalog.entries)} Codex models available.")
        print("Start astra, then select ChatGPT / Codex in /model.")
    else:
        raise ValueError("Use astra auth login, status or logout.")
    return 0


def main() -> int:
    try:
        return asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "status"))
    except KeyboardInterrupt:
        print("ChatGPT login cancelled.", file=sys.stderr)
        return 130
    except TimeoutError:
        print("ChatGPT device login expired. Run astra auth login again.", file=sys.stderr)
        return 1
    except (ValueError, OSError, httpx.HTTPError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not reach OpenAI or save Astra credentials."
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
