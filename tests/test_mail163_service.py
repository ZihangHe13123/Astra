from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Self

import pytest

from agent.mail163.imap_client import MailAuthenticationError, MailTlsError, MailUnsafeLoginError
from agent.mail163.mime import MailProtocolError
from agent.mail163.models import AttachmentMeta, FolderSnapshot, MailConfig, RemoteMessage
from agent.mail163.service import MailError, MailService
from agent.mail163.store import MailStore

ACCOUNT = "user@163.com"


def message(
    folder: str,
    uidvalidity: int,
    uid: int,
    *,
    subject: str,
    received_at: float | None = None,
    body_truncated: bool = False,
    attachments: tuple[AttachmentMeta, ...] = (),
) -> RemoteMessage:
    return RemoteMessage(
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
        message_id=f"<{folder}-{uidvalidity}-{uid}@example>",
        sender="sender@example.com",
        recipients=ACCOUNT,
        subject=subject,
        sent_at="Thu, 21 Aug 2026 10:00:00 +0800",
        received_at=float(uid) if received_at is None else received_at,
        flags=(),
        size=120,
        body_text=f"body {uid}",
        body_truncated=body_truncated,
        attachments=attachments,
        warnings=(),
    )


class ScriptedClient:
    def __init__(
        self,
        *,
        snapshots: dict[str, FolderSnapshot],
        messages: dict[tuple[str, int], RemoteMessage] | None = None,
        folder_errors: dict[str, Exception] | None = None,
        attachment_bytes: dict[tuple[str, int, int, str], bytes] | None = None,
        folder_names: tuple[str, ...] = ("INBOX", "已发送", "草稿箱"),
        enter_error: Exception | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.messages = {} if messages is None else messages
        self.folder_errors = {} if folder_errors is None else folder_errors
        self.attachment_bytes = {} if attachment_bytes is None else attachment_bytes
        self.folder_names = folder_names
        self.enter_error = enter_error
        self.snapshot_calls: list[str] = []
        self.fetch_calls: list[tuple[str, set[int]]] = []
        self.attachment_calls: list[tuple[str, int, int, str]] = []
        self.list_calls = 0
        self.enter_calls = 0
        self.exit_calls = 0

    def __enter__(self) -> Self:
        self.enter_calls += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        del exc_type, exc_value, traceback
        self.exit_calls += 1
        return False

    def snapshot(self, folder: str) -> FolderSnapshot:
        self.snapshot_calls.append(folder)
        if folder in self.folder_errors:
            raise self.folder_errors[folder]
        return self.snapshots[folder]

    def fetch_messages(
        self,
        folder: str,
        snapshot: FolderSnapshot,
        uids: set[int],
    ) -> tuple[RemoteMessage, ...]:
        assert snapshot is self.snapshots[folder]
        requested = set(uids)
        self.fetch_calls.append((folder, requested))
        return tuple(self.messages[(folder, uid)] for uid in sorted(requested))

    def list_folders(self) -> tuple[str, ...]:
        self.list_calls += 1
        return self.folder_names

    def download_attachment(self, folder: str, uidvalidity: int, uid: int, part_id: str) -> bytes:
        key = (folder, uidvalidity, uid, part_id)
        self.attachment_calls.append(key)
        return self.attachment_bytes[key]


class ClientFactory:
    def __init__(self, client: ScriptedClient) -> None:
        self.client = client
        self.calls: list[MailConfig] = []

    def __call__(self, config: MailConfig) -> ScriptedClient:
        self.calls.append(config)
        return self.client


@pytest.fixture
def config(tmp_path: Path) -> MailConfig:
    mail_dir = tmp_path / ".astra" / "mail"
    return MailConfig(
        project_root=tmp_path,
        account=ACCOUNT,
        auth_code="super-secret-auth-code",
        host="imap.163.com",
        port=993,
        database_path=mail_dir / "163.sqlite3",
        attachments_dir=mail_dir / "attachments",
        max_body_bytes=64,
        default_folders=("INBOX", "已发送"),
    )


@pytest.fixture
def seeded_store(config: MailConfig) -> MailStore:
    store = MailStore(config.database_path)
    inbox = (
        message("INBOX", 10, 1, subject="old inbox", received_at=101.0),
        message(
            "INBOX",
            10,
            2,
            subject="cached inbox",
            received_at=102.0,
            attachments=(AttachmentMeta("2", "../../report.pdf", "application/pdf", 9),),
        ),
    )
    sent = (message("已发送", 20, 8, subject="cached sent", received_at=98.0),)
    store.apply_folder_sync(ACCOUNT, "INBOX", 10, {1, 2}, inbox, {}, 100.0)
    store.apply_folder_sync(ACCOUNT, "已发送", 20, {8}, sent, {}, 90.0)
    return store


def default_client() -> ScriptedClient:
    snapshots = {
        "INBOX": FolderSnapshot("INBOX", 10, frozenset({2, 3}), {2: ("\\Seen",), 3: ()}),
        "已发送": FolderSnapshot("已发送", 20, frozenset({8, 9}), {8: (), 9: ()}),
    }
    messages = {
        ("INBOX", 3): message(
            "INBOX",
            10,
            3,
            subject="new inbox",
            received_at=303.0,
            body_truncated=True,
        ),
        ("已发送", 9): message("已发送", 20, 9, subject="new sent", received_at=209.0),
    }
    return ScriptedClient(
        snapshots=snapshots,
        messages=messages,
        attachment_bytes={("INBOX", 10, 2, "2"): b"pdf bytes"},
    )


@pytest.fixture
def fake_client_factory() -> ClientFactory:
    return ClientFactory(default_client())


@pytest.fixture
def partial_client_factory() -> ClientFactory:
    client = default_client()
    client.folder_errors["已发送"] = MailError(
        "folder_unavailable",
        "Mailbox folder is unavailable",
        4,
    )
    return ClientFactory(client)


@pytest.fixture
def failing_client_factory() -> ClientFactory:
    return ClientFactory(
        ScriptedClient(
            snapshots={},
            enter_error=OSError("connection failed: super-secret-auth-code"),
        )
    )


def test_sync_fetches_only_unknown_uids(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    service = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
        clock=lambda: 300.0,
    )

    report = service.sync()

    assert report.status == "ok"
    assert fake_client_factory.client.snapshot_calls == ["INBOX", "已发送"]
    assert fake_client_factory.client.fetch_calls == [("INBOX", {3}), ("已发送", {9})]
    assert fake_client_factory.client.attachment_calls == []
    assert fake_client_factory.client.enter_calls == 1
    assert fake_client_factory.client.exit_calls == 1
    cached = seeded_store.read(config.account, "INBOX", 10, 3)
    assert cached is not None
    assert cached["subject"] == "new inbox"
    assert cached["body_truncated"] is True


def test_partial_failure_commits_inbox_and_preserves_sent_cursor(
    config: MailConfig,
    seeded_store: MailStore,
    partial_client_factory: ClientFactory,
) -> None:
    before = seeded_store.folder_state(config.account, "已发送")

    report = MailService(
        config,
        store=seeded_store,
        client_factory=partial_client_factory,
        clock=lambda: 300.0,
    ).sync()

    assert report.status == "partial"
    assert report.folders[0].status == "ok"
    assert report.folders[1].error_code == "folder_unavailable"
    after = seeded_store.folder_state(config.account, "已发送")
    assert before is not None and after is not None
    assert after.last_success == before.last_success
    assert after.last_error_code == "folder_unavailable"
    assert seeded_store.read(config.account, "INBOX", 10, 3) is not None


def test_refresh_failure_returns_labelled_existing_cache(
    config: MailConfig,
    seeded_store: MailStore,
    failing_client_factory: ClientFactory,
) -> None:
    result = MailService(
        config,
        store=seeded_store,
        client_factory=failing_client_factory,
    ).recent(refresh=True, limit=10)

    assert result["cache_used"] is True
    assert result["sync_status"] == "failed"
    assert result["last_success"] == 100.0
    assert result["items"]
    assert result["errors"][0]["code"] == "network_error"  # type: ignore[index]
    assert config.auth_code not in str(result)


def test_refresh_failure_uses_prior_successful_empty_folder_cache(
    config: MailConfig,
    failing_client_factory: ClientFactory,
) -> None:
    store = MailStore(config.database_path)
    store.apply_folder_sync(config.account, "INBOX", 10, set(), [], {}, 100.0)

    result = MailService(
        config,
        store=store,
        client_factory=failing_client_factory,
    ).recent(folder="INBOX", refresh=True)

    assert result["items"] == []
    assert result["cache_used"] is True
    assert result["sync_status"] == "failed"
    assert result["last_success"] == 100.0


def test_partial_refresh_returns_legitimately_empty_successful_folder(
    config: MailConfig,
) -> None:
    store = MailStore(config.database_path)
    client = ScriptedClient(
        snapshots={"INBOX": FolderSnapshot("INBOX", 10, frozenset(), {})},
        folder_errors={
            "已发送": MailError(
                "folder_unavailable",
                "Mailbox folder is unavailable",
                4,
            )
        },
    )

    result = MailService(
        config,
        store=store,
        client_factory=ClientFactory(client),
        clock=lambda: 300.0,
    ).recent(folder="INBOX", refresh=True)

    assert result["items"] == []
    assert result["sync_status"] == "partial"
    assert result["last_success"] == 300.0
    assert result["errors"][0]["folder"] == "已发送"  # type: ignore[index]


def test_partial_multifolder_search_accepts_successful_empty_scope(
    config: MailConfig,
) -> None:
    store = MailStore(config.database_path)
    client = ScriptedClient(
        snapshots={"INBOX": FolderSnapshot("INBOX", 10, frozenset(), {})},
        folder_errors={
            "已发送": MailError(
                "folder_unavailable",
                "Mailbox folder is unavailable",
                4,
            )
        },
    )

    result = MailService(
        config,
        store=store,
        client_factory=ClientFactory(client),
        clock=lambda: 300.0,
    ).search(refresh=True)

    assert result["items"] == []
    assert result["cache_used"] is True
    assert result["sync_status"] == "partial"
    assert result["last_success"] == 300.0
    assert result["errors"][0]["folder"] == "已发送"  # type: ignore[index]


def test_zero_cache_refresh_failure_raises_no_cache_and_records_first_sync_error(
    config: MailConfig,
    failing_client_factory: ClientFactory,
) -> None:
    store = MailStore(config.database_path)
    service = MailService(config, store=store, client_factory=failing_client_factory)

    with pytest.raises(MailError) as caught:
        service.recent(refresh=True)

    assert (caught.value.code, caught.value.exit_code) == ("no_cache", 4)
    states = store.folder_states(config.account)
    assert [state["folder"] for state in states] == ["INBOX", "已发送"]
    assert all(state["uidvalidity"] == 0 and state["last_success"] is None for state in states)
    assert all(state["last_error_code"] == "network_error" for state in states)


def test_offline_query_uses_existing_cache_without_constructing_client(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    assert seeded_store.exists()
    offline_config = replace(config, account="", auth_code="")
    service = MailService(offline_config, client_factory=fake_client_factory)

    result = service.search("cached", folder="sent", refresh=False)

    assert [item["uid"] for item in result["items"]] == [8]  # type: ignore[index]
    assert result["cache_used"] is True
    assert result["sync_status"] == "offline"
    assert fake_client_factory.calls == []


def test_offline_first_sync_error_sentinel_is_not_usable_cache(
    config: MailConfig,
    fake_client_factory: ClientFactory,
) -> None:
    store = MailStore(config.database_path)
    store.record_folder_error(config.account, "INBOX", "network_error", "Unable to reach imap.163.com")
    offline_config = replace(config, account="", auth_code="")

    with pytest.raises(MailError) as caught:
        MailService(
            offline_config,
            client_factory=fake_client_factory,
        ).recent(folder="INBOX", refresh=False)

    assert (caught.value.code, caught.value.exit_code) == ("no_cache", 4)
    state = store.folder_state(config.account, "INBOX")
    assert state is not None
    assert state.uidvalidity == 0
    assert state.last_success is None
    assert fake_client_factory.calls == []


def test_offline_missing_cache_does_not_create_local_state(
    config: MailConfig,
    fake_client_factory: ClientFactory,
) -> None:
    offline_config = replace(config, account="", auth_code="")
    service = MailService(offline_config, client_factory=fake_client_factory)

    assert not config.database_path.exists()

    with pytest.raises(MailError) as caught:
        service.recent(refresh=False)

    assert caught.value.code == "no_cache"
    assert not config.database_path.exists()
    assert not config.database_path.parent.exists()
    assert fake_client_factory.calls == []


def test_sync_reports_remote_removal_without_deleting_cached_message(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    report = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
        clock=lambda: 300.0,
    ).sync(["INBOX"])

    assert report.folders[0].remote_removed == 1
    removed = seeded_store.read(config.account, "INBOX", 10, 1)
    assert removed is not None
    assert removed["remote_removed"] is True


def test_sync_uidvalidity_reset_retains_old_generation(
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    replacement = message("INBOX", 11, 1, subject="new generation", received_at=401.0)
    client = ScriptedClient(
        snapshots={"INBOX": FolderSnapshot("INBOX", 11, frozenset({1}), {1: ()})},
        messages={("INBOX", 1): replacement},
    )

    report = MailService(
        config,
        store=seeded_store,
        client_factory=ClientFactory(client),
        clock=lambda: 400.0,
    ).sync(["INBOX"])

    assert report.status == "ok"
    assert report.folders[0].fetched == 1
    assert report.folders[0].remote_removed == 2
    assert seeded_store.read(config.account, "INBOX", 10, 1)["remote_removed"] is True  # type: ignore[index]
    assert seeded_store.read(config.account, "INBOX", 11, 1)["subject"] == "new generation"  # type: ignore[index]


def test_sync_resolves_explicit_folder_alias(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    report = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
        clock=lambda: 300.0,
    ).sync(["sent"])

    assert [result.folder for result in report.folders] == ["已发送"]
    assert fake_client_factory.client.snapshot_calls == ["已发送"]


def test_query_refresh_synchronizes_an_explicit_non_default_folder(
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    draft = message("草稿箱", 30, 5, subject="new draft", received_at=305.0)
    client = ScriptedClient(
        snapshots={"草稿箱": FolderSnapshot("草稿箱", 30, frozenset({5}), {5: ()})},
        messages={("草稿箱", 5): draft},
    )

    result = MailService(
        config,
        store=seeded_store,
        client_factory=ClientFactory(client),
        clock=lambda: 300.0,
    ).recent(folder="草稿箱", refresh=True)

    assert [item["uid"] for item in result["items"]] == [5]  # type: ignore[index]
    assert client.snapshot_calls == ["草稿箱"]
    assert client.fetch_calls == [("草稿箱", {5})]
    assert result["sync_status"] == "ok"


def test_search_refreshes_before_query_and_forwards_filters(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    result = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
        clock=lambda: 300.0,
    ).search("new", sender="sender", folder="inbox", limit=5, window=10)

    assert [item["uid"] for item in result["items"]] == [3]  # type: ignore[index]
    assert result["sync_status"] == "ok"
    assert result["cache_used"] is False
    assert fake_client_factory.client.snapshot_calls == ["INBOX", "已发送"]


def test_read_resolves_current_folder_generation_when_uidvalidity_is_omitted(
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    replacement = replace(
        message("INBOX", 10, 2, subject="old generation"),
        uidvalidity=11,
        subject="current generation",
    )
    seeded_store.apply_folder_sync(config.account, "INBOX", 11, {2}, [replacement], {}, 200.0)

    result = MailService(config, store=seeded_store).read("inbox", 2, refresh=False)

    assert result["item"]["subject"] == "current generation"  # type: ignore[index]
    assert result["item"]["uidvalidity"] == 11  # type: ignore[index]


def test_read_missing_message_raises_stable_not_found_error(
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    with pytest.raises(MailError) as caught:
        MailService(config, store=seeded_store).read("INBOX", 999, refresh=False)

    assert (caught.value.code, caught.value.exit_code) == ("message_not_found", 4)


def test_read_first_refresh_failure_raises_no_cache(
    config: MailConfig,
    failing_client_factory: ClientFactory,
) -> None:
    store = MailStore(config.database_path)

    with pytest.raises(MailError) as caught:
        MailService(
            config,
            store=store,
            client_factory=failing_client_factory,
        ).read("INBOX", 1, refresh=True)

    assert (caught.value.code, caught.value.exit_code) == ("no_cache", 4)
    state = store.folder_state(config.account, "INBOX")
    assert state is not None
    assert state.uidvalidity == 0
    assert state.last_success is None


def test_folders_merges_remote_names_with_local_sync_state(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    result = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
    ).folders(refresh=True)

    assert result["items"][0]["folder"] == "INBOX"  # type: ignore[index]
    assert result["items"][0]["last_success"] == 100.0  # type: ignore[index]
    assert {item["folder"] for item in result["items"]} >= {"INBOX", "已发送", "草稿箱"}  # type: ignore[union-attr]
    assert result["sync_status"] == "ok"
    assert fake_client_factory.client.list_calls == 1


def test_folders_refresh_failure_returns_labelled_local_states(
    config: MailConfig,
    seeded_store: MailStore,
    failing_client_factory: ClientFactory,
) -> None:
    result = MailService(
        config,
        store=seeded_store,
        client_factory=failing_client_factory,
    ).folders(refresh=True)

    assert {item["folder"] for item in result["items"]} == {"INBOX", "已发送"}  # type: ignore[union-attr]
    assert result["cache_used"] is True
    assert result["sync_status"] == "failed"
    assert result["errors"][0]["code"] == "network_error"  # type: ignore[index]


def test_status_reports_local_folder_states_without_remote_access(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    result = MailService(config, store=seeded_store, client_factory=fake_client_factory).status()

    assert result["sync_status"] == "offline"
    assert result["cache_used"] is True
    assert {item["folder"] for item in result["items"]} == {"INBOX", "已发送"}  # type: ignore[union-attr]
    assert result["last_success"] == 100.0
    assert fake_client_factory.calls == []


def test_status_preserves_first_sync_error_sentinel(
    config: MailConfig,
) -> None:
    store = MailStore(config.database_path)
    store.record_folder_error(config.account, "已发送", "folder_unavailable", "Mailbox folder is unavailable")

    result = MailService(config, store=store).status()

    assert result["items"] == [
        {
            "folder": "已发送",
            "uidvalidity": 0,
            "last_success": None,
            "last_error_code": "folder_unavailable",
            "last_error_message": "Mailbox folder is unavailable",
        }
    ]


def test_attachment_download_requires_known_metadata(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    with pytest.raises(MailError) as caught:
        MailService(
            config,
            store=seeded_store,
            client_factory=fake_client_factory,
        ).download_attachment("INBOX", 10, 2, "99")

    assert caught.value.code == "attachment_not_found"
    assert fake_client_factory.calls == []


def test_attachment_download_uses_safe_owner_only_target(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    result = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
    ).download_attachment(folder="INBOX", uidvalidity=10, uid=2, part_id="2")

    path = result["path"]
    assert isinstance(path, Path)
    assert path.is_relative_to(config.attachments_dir)
    assert path.name == "report.pdf"
    assert path.read_bytes() == b"pdf bytes"
    assert fake_client_factory.client.attachment_calls == [("INBOX", 10, 2, "2")]
    cached = seeded_store.attachment(config.account, "INBOX", 10, 2, "2")
    assert cached is not None and cached["downloaded_path"] == str(path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_attachment_download_collision_adds_deterministic_suffix(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    target_dir = config.attachments_dir / "INBOX" / "10" / "2"
    target_dir.mkdir(parents=True)
    (target_dir / "report.pdf").write_bytes(b"existing")

    result = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
    ).download_attachment("INBOX", 10, 2, "2")

    assert result["path"].name == "report (1).pdf"  # type: ignore[union-attr]
    assert (target_dir / "report.pdf").read_bytes() == b"existing"
    assert result["path"].read_bytes() == b"pdf bytes"  # type: ignore[union-attr]


@pytest.mark.skipif(os.name != "nt", reason="Windows attachment storage contract")
def test_attachment_download_on_windows_succeeds_and_updates_cache(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    result = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
    ).download_attachment("INBOX", 10, 2, "2")

    path = result["path"]
    assert isinstance(path, Path)
    assert path.read_bytes() == b"pdf bytes"
    assert fake_client_factory.client.attachment_calls == [("INBOX", 10, 2, "2")]
    cached = seeded_store.attachment(config.account, "INBOX", 10, 2, "2")
    assert cached is not None and cached["downloaded_path"] == str(path)


def test_attachment_publication_never_overwrites_file_created_after_reservation(
    monkeypatch: pytest.MonkeyPatch,
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    service = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
    )
    real_atomic_write = service._atomic_write
    injected_collision = False

    def create_collision_then_publish(target: object, payload: bytes) -> None:
        nonlocal injected_collision
        if not injected_collision:
            injected_collision = True
            descriptor = service._open_target_file(
                target,  # type: ignore[arg-type]
                target.name,  # type: ignore[attr-defined]
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"concurrent file")
        real_atomic_write(target, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_atomic_write", create_collision_then_publish)

    result = service.download_attachment("INBOX", 10, 2, "2")

    target_dir = config.attachments_dir / "INBOX" / "10" / "2"
    assert (target_dir / "report.pdf").read_bytes() == b"concurrent file"
    assert result["path"].name == "report (1).pdf"  # type: ignore[union-attr]
    assert result["path"].read_bytes() == b"pdf bytes"  # type: ignore[union-attr]
    assert fake_client_factory.client.attachment_calls == [("INBOX", 10, 2, "2")]


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink safety contract")
def test_attachment_download_rejects_symlinked_directory_component(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    outside_mode = stat.S_IMODE(outside.stat().st_mode)
    config.attachments_dir.mkdir(parents=True)
    (config.attachments_dir / "INBOX").symlink_to(outside, target_is_directory=True)

    with pytest.raises(MailError) as caught:
        MailService(
            config,
            store=seeded_store,
            client_factory=fake_client_factory,
        ).download_attachment("INBOX", 10, 2, "2")

    assert (caught.value.code, caught.value.exit_code) == ("storage_error", 5)
    assert list(outside.iterdir()) == []
    assert stat.S_IMODE(outside.stat().st_mode) == outside_mode
    cached = seeded_store.attachment(config.account, "INBOX", 10, 2, "2")
    assert cached is not None and cached["downloaded_path"] == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point safety contract")
def test_attachment_download_rejects_junction_directory_component(
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    config.attachments_dir.mkdir(parents=True)
    junction = config.attachments_dir / "INBOX"
    completed = subprocess.run(
        ["cmd.exe", "/D", "/C", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    try:
        with pytest.raises(MailError) as caught:
            MailService(
                config,
                store=seeded_store,
                client_factory=fake_client_factory,
            ).download_attachment("INBOX", 10, 2, "2")

        assert (caught.value.code, caught.value.exit_code) == ("storage_error", 5)
        assert list(outside.iterdir()) == []
        cached = seeded_store.attachment(config.account, "INBOX", 10, 2, "2")
        assert cached is not None and cached["downloaded_path"] == ""
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory pinning contract")
def test_attachment_windows_pins_target_directory_against_swap(
    config: MailConfig,
    seeded_store: MailStore,
    tmp_path: Path,
) -> None:
    service = MailService(config, store=seeded_store)
    target = service._attachment_target("INBOX", 10, 2, "report.pdf")
    parked = target.path.parent.with_name(f"{target.path.parent.name}.parked")
    outside = tmp_path / "outside-swap"
    outside.mkdir()
    junction = target.path.parent
    try:
        junction.rename(parked)
        completed = subprocess.run(
            ["cmd.exe", "/D", "/C", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("Windows junction creation is unavailable")
        service._atomic_write(target, b"payload")
        assert target.path.parent == parked
        assert target.path.read_bytes() == b"payload"
        assert list(outside.iterdir()) == []
    finally:
        if not target.released:
            service._release_attachment_target(target, remove_file=True)
        if junction.is_dir():
            junction.rmdir()


def test_attachment_target_reservation_avoids_concurrent_collision(
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    service = MailService(config, store=seeded_store)

    first = service._attachment_target("INBOX", 10, 2, "report.pdf")
    second = service._attachment_target("INBOX", 10, 2, "report.pdf")

    assert first.name == "report.pdf"
    assert second.name == "report (1).pdf"
    assert not first.exists()
    assert not second.exists()
    assert service._reservation_path(first).exists()
    assert service._reservation_path(second).exists()
    service._release_attachment_target(first)
    service._release_attachment_target(second)


def test_attachment_target_check_failure_removes_reservation(
    monkeypatch: pytest.MonkeyPatch,
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    service = MailService(config, store=seeded_store)
    monkeypatch.setattr(
        service,
        "_target_entry_exists",
        lambda target, name: (_ for _ in ()).throw(OSError("stat failed")),
    )

    with pytest.raises(OSError, match="stat failed"):
        service._attachment_target("INBOX", 10, 2, "report.pdf")

    assert list(config.attachments_dir.rglob(".*.astra-download")) == []


def test_attachment_release_attempts_all_cleanup_after_unlink_error(
    monkeypatch: pytest.MonkeyPatch,
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    service = MailService(config, store=seeded_store)
    target = service._attachment_target("INBOX", 10, 2, "report.pdf")
    temporary_name = f".{target.name}.test.tmp"
    descriptor = service._open_target_file(
        target,
        temporary_name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    os.close(descriptor)
    real_unlink = service._unlink_target_entry
    attempts: list[str] = []

    def fail_temporary_once(attachment_target: object, name: str) -> None:
        attempts.append(name)
        if name == temporary_name:
            raise OSError("cleanup blocked")
        real_unlink(attachment_target, name)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_unlink_target_entry", fail_temporary_once)

    with pytest.raises(OSError, match="cleanup blocked"):
        service._release_attachment_target(target, temporary_name=temporary_name)

    assert target.reservation_name in attempts
    assert not service._reservation_path(target).exists()
    assert target.released is True


def test_attachment_local_write_failure_is_a_sanitized_storage_error(
    monkeypatch: pytest.MonkeyPatch,
    config: MailConfig,
    seeded_store: MailStore,
    fake_client_factory: ClientFactory,
) -> None:
    service = MailService(
        config,
        store=seeded_store,
        client_factory=fake_client_factory,
    )
    monkeypatch.setattr(
        service,
        "_atomic_write",
        lambda target, payload: (_ for _ in ()).throw(OSError(f"disk full: {config.auth_code}")),
    )

    with pytest.raises(MailError) as caught:
        service.download_attachment("INBOX", 10, 2, "2")

    assert (caught.value.code, caught.value.safe_message, caught.value.exit_code) == (
        "storage_error",
        "Local attachment write failed",
        5,
    )
    assert config.auth_code not in str(caught.value)
    cached = seeded_store.attachment(config.account, "INBOX", 10, 2, "2")
    assert cached is not None and cached["downloaded_path"] == ""
    assert list(config.attachments_dir.rglob(".*.astra-download")) == []


def test_attachment_temp_creation_failure_removes_reservation(
    monkeypatch: pytest.MonkeyPatch,
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    service = MailService(config, store=seeded_store)
    target = service._attachment_target("INBOX", 10, 2, "report.pdf")
    reservation = service._reservation_path(target)
    real_open = service._open_target_file

    def fail_temporary(
        attachment_target: object,
        name: str,
        flags: int,
        mode: int,
    ) -> int:
        if name.endswith(".tmp"):
            raise OSError("no temporary file")
        return real_open(attachment_target, name, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_open_target_file", fail_temporary)

    with pytest.raises(OSError, match="temporary"):
        service._atomic_write(target, b"payload")

    assert not target.exists()
    assert not reservation.exists()


def test_sync_redacts_authorization_code_from_unexpected_error(
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    client = default_client()
    client.folder_errors["INBOX"] = RuntimeError(f"server echoed {config.auth_code}")
    report = MailService(
        config,
        store=seeded_store,
        client_factory=ClientFactory(client),
    ).sync(["INBOX"])

    assert report.status == "failed"
    assert report.folders[0].error_code == "sync_failed"
    assert report.folders[0].error_message == "Mail synchronization failed"
    assert config.auth_code not in str(report)


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_message"),
    [
        (MailTlsError("certificate detail: super-secret-auth-code"), "tls_error", "TLS connection to imap.163.com failed"),
        (
            MailUnsafeLoginError("unsafe detail: super-secret-auth-code"),
            "unsafe_login",
            "The mail server rejected the login as unsafe",
        ),
        (
            MailAuthenticationError("auth detail: super-secret-auth-code"),
            "auth_failed",
            "Mail login failed",
        ),
        (
            MailProtocolError("IMAP ID failed: super-secret-auth-code"),
            "protocol_error",
            "The mail server response could not be processed",
        ),
    ],
)
def test_service_preserves_stable_protocol_error_classifications(
    error: Exception,
    expected_code: str,
    expected_message: str,
    config: MailConfig,
    seeded_store: MailStore,
) -> None:
    client = ScriptedClient(snapshots={}, enter_error=error)

    report = MailService(
        config,
        store=seeded_store,
        client_factory=ClientFactory(client),
    ).sync(["INBOX"])

    assert report.status == "failed"
    assert report.folders[0].error_code == expected_code
    assert report.folders[0].error_message == expected_message
    assert report.folders[0].synced_at is None
    assert config.auth_code not in str(report)


def test_a_large_mailbox_caches_its_newest_messages_first_and_only_newer_ones_later(config: MailConfig) -> None:
    settings = replace(config, sync_limit=5, default_folders=("INBOX",))
    uids = frozenset(range(1, 101))
    client = ScriptedClient(
        snapshots={"INBOX": FolderSnapshot("INBOX", 10, uids, {uid: () for uid in uids})},
        messages={("INBOX", uid): message("INBOX", 10, uid, subject=f"m{uid}") for uid in range(1, 104)},
    )
    service = MailService(settings, client_factory=ClientFactory(client))
    service.sync()
    assert client.fetch_calls[-1] == ("INBOX", {96, 97, 98, 99, 100})
    newer = frozenset(range(1, 104))
    client.snapshots["INBOX"] = FolderSnapshot("INBOX", 10, newer, {uid: () for uid in newer})
    service.sync()
    # Two new messages arrived; the 95 older ones are not backfilled.
    assert client.fetch_calls[-1] == ("INBOX", {101, 102, 103})
    assert [item["uid"] for item in service.recent(refresh=False, limit=50)["items"]][:3] == [103, 102, 101]
