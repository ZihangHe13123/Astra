"""Durable references used both by the mail tool and by an event waiting for a worker."""

import hashlib
import json
import re

from .config import MAIN_ACCOUNT
from .models import MailConfig


_REF = re.compile(r"(.*):(\d+):(\d+)(?:@([A-Za-z0-9]+)(?:~([0-9a-f]{32}))?)?", re.DOTALL)


def mailbox_identity(config: MailConfig) -> str:
    parts = (config.host.casefold(), config.port, config.account or config.address)
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


def message_ref(config: MailConfig, folder: str, generation: int, uid: int) -> str:
    return f"{folder}:{generation}:{uid}@{config.name}~{mailbox_identity(config)}"


def parse_ref(ref: str) -> tuple[str, str, int, int, str | None]:
    match = _REF.fullmatch(ref)
    if match is None or not match[1]:
        raise ValueError(f"ref {ref!r} is not a ref from recent or search.")
    return (match[4] or MAIN_ACCOUNT).casefold(), match[1], int(match[2]), int(match[3]), match[5]
