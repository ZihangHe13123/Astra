import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from test_jobs_delivery import at, check, finish
from test_weixin_channel import adapter, logged_in, until
from weixin_fixture import OWNER, FakeIlink
from agent.channels.job_push import weixin_job_push
from agent.channels.config import WeixinConfig
from agent.channels.weixin import IlinkClient, WeixinAccount, WeixinAdapter, WeixinStore
from agent.runtime.jobs.calendar import set_quiet_hours
from agent.runtime.jobs.delivery import JobDelivery, RETRY_SECONDS
from agent.runtime.jobs.store import JobStore


@pytest.fixture
def store(tmp_path):
    jobs = JobStore(tmp_path / "jobs")
    set_quiet_hours(jobs.home, None)
    return jobs


def test_retry_never_resends_an_already_acknowledged_wechat_piece(tmp_path, store):
    async def scenario():
        server = FakeIlink()
        channel = None
        try:
            config_store = WeixinStore(tmp_path / "test-login")
            await logged_in(server, config_store)
            channel = adapter(server, config_store, AsyncMock(), max_output_chars=200)
            await channel.start()
            await until(lambda: channel.running, "local fixture online")
            now = at("2026-11-18T12:01")
            check(store, tmp_path, next_at=now, targets=["weixin"])
            run = finish(store, now, output="111111" * 26 + "\n\n" + "222222" * 26)
            server.send_answers.put({"ret": 0})
            server.send_answers.put({"ret": -2, "errmsg": "unknown error"})
            push = weixin_job_push(SimpleNamespace(adapter=lambda _: channel),
                                   SimpleNamespace(record_outbound=AsyncMock()))
            delivery = JobDelivery(store, desktop=None, weixin=push)
            await delivery.deliver_due(now=now)
            first_attempt = server.replies_to(OWNER)
            assert len(first_attempt) == 2
            assert [record["state"] for record in store.pushes(run["id"]) if record["target"] == "weixin"] == ["pending"]
            await delivery.deliver_due(now=now + RETRY_SECONDS[0])
            requests = server.replies_to(OWNER)
            assert requests.count(first_attempt[0]) == 1, [first_attempt.index(request) + 1 for request in requests]
        finally:
            if channel is not None:
                await channel.stop()
            server.close()
    asyncio.run(scenario())


@asynccontextmanager
async def scheduled_channel(tmp_path, store, answers, *, pause=lambda _: 0, output=None):
    sent = []

    async def respond(request):
        message = json.loads(request.content)["msg"]
        sent.append((message["to_user_id"], message["item_list"][0]["text_item"]["text"]))
        answer = answers.pop(0) if answers else {"ret": 0}
        if isinstance(answer, type):
            raise answer("fixture", request=request)
        return httpx.Response(200, json=answer)

    client = IlinkClient(transport=httpx.MockTransport(respond))
    try:
        channel = WeixinAdapter(WeixinConfig(enabled=True, max_output_chars=200), AsyncMock(),
                                store=WeixinStore(tmp_path / "weixin"), client=client, pause=pause)
        def login(account, owner=OWNER):
            channel._account = WeixinAccount(account, "test-token", "https://fixture.invalid", owner)
            channel._state = "online"
        login("original-bot")
        recorder = AsyncMock()
        push = weixin_job_push(SimpleNamespace(adapter=lambda _: channel), SimpleNamespace(record_outbound=recorder))
        now = at("2026-11-18T12:01")
        check(store, tmp_path, next_at=now, targets=["weixin"])
        run = finish(store, now, output=output or ("first-" * 25 + "\n\n" + "second-" * 25))
        async def deliver(attempt):
            reopened = JobStore(store.home)
            await JobDelivery(reopened, desktop=None, weixin=push).deliver_due(now=now + attempt * RETRY_SECONDS[0])
            return next(row for row in reopened.pushes(run["id"]) if row["target"] == "weixin")["state"]
        yield channel, login, deliver, sent, recorder, run
    finally:
        await client.close()


@pytest.mark.parametrize("first,replacement_owner,delivered", [
    (httpx.ConnectError, OWNER, True), ({"ret": -2, "errmsg": "unknown error"}, OWNER, True),
    (httpx.ConnectError, "different-person", False), (httpx.ReadTimeout, OWNER, False),
])
def test_relogin_keeps_the_recipient_and_retries_only_a_known_unsent_delivery(
        store, tmp_path, first, replacement_owner, delivered):
    async def scenario():
        async with scheduled_channel(tmp_path, store, [first]) as (_, login, deliver, sent, recorder, _run):
            assert await deliver(0) == "pending"
            login("replacement-bot", replacement_owner)
            assert await deliver(1) == ("sent" if delivered else "failed")
            assert len(sent) == (3 if delivered else 1)
            assert {person for person, _ in sent} == {OWNER}
            assert recorder.await_count == int(delivered)
    asyncio.run(scenario())


def test_sender_change_after_partial_ack_never_continues_under_new_login(tmp_path, store):
    async def scenario():
        async with scheduled_channel(tmp_path, store, [{"ret": 0}, httpx.ConnectError]) as (
                _channel, login, deliver, sent, recorder, _run):
            assert await deliver(0) == "pending"
            assert len(sent) == 2
            login("replacement-bot")
            assert await deliver(1) == "failed"
            assert len(sent) == 2
            recorder.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("sender,expected", [("original-bot", "sent"), ("replacement-bot", "failed")])
def test_legacy_unsent_full_plan_requires_its_original_sender(tmp_path, store, sender, expected):
    async def scenario():
        async with scheduled_channel(tmp_path, store, [httpx.ReadTimeout]) as (
                _channel, login, deliver, sent, _recorder, run):
            assert await deliver(0) == "pending"
            with store.transaction() as db:
                # v5's old explicit rejection cleared the in-flight bit but left the full plan hash.
                db.execute("UPDATE pushes SET sending_piece=0 WHERE run_id=? AND target='weixin'", (run["id"],))
            login(sender)
            assert await deliver(1) == expected
            assert len(sent) == (3 if expected == "sent" else 1)
    asyncio.run(scenario())


def test_offline_known_recipient_is_not_replaced_at_the_next_attempt(tmp_path, store):
    async def scenario():
        async with scheduled_channel(tmp_path, store, []) as (channel, login, deliver, sent, recorder, _run):
            channel._state = "standby"
            assert await deliver(0) == "pending"
            login("different-bot", "different-person")
            assert await deliver(1) == "failed"
            assert not sent
            recorder.assert_not_awaited()
    asyncio.run(scenario())


def test_cancelling_between_chunks_does_not_reserve_or_delay_the_first_remaining_send(tmp_path, store):
    async def scenario():
        pauses = []
        started = asyncio.Event()
        def pause(piece):
            pauses.append(piece)
            started.set()
            return 30
        async with scheduled_channel(tmp_path, store, [], pause=pause) as (
                _channel, _login, deliver, sent, recorder, _run):
            first = asyncio.create_task(deliver(0))
            try:
                await asyncio.wait_for(started.wait(), 10)
                assert len(sent) == 1
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            assert await asyncio.wait_for(deliver(1), 10) == "sent"
            assert len(sent) == 2 and pauses == [sent[1][1]]
            recorder.assert_awaited_once()
    asyncio.run(scenario())


def test_each_consecutive_send_is_paced_only_after_the_previous_one(tmp_path, store):
    async def scenario():
        pauses = []
        def pause(piece):
            pauses.append((len(sent), piece))
            return 0
        text = "\n\n".join(char * 175 for char in "ABC")
        async with scheduled_channel(tmp_path, store, [], pause=pause, output=text) as (
                _channel, _login, deliver, sent, recorder, _run):
            assert await deliver(0) == "sent"
            assert len(sent) == 3 and pauses == [(1, sent[1][1]), (2, sent[2][1])]
            recorder.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("error,safe_to_retry", [
    (httpx.PoolTimeout, True), (httpx.ConnectTimeout, True), (httpx.ConnectError, True),
    (httpx.ReadTimeout, False), (httpx.WriteError, False),
])
def test_job_push_replays_only_failures_before_the_request_was_sent(tmp_path, store, error, safe_to_retry):
    async def scenario():
        requests = []
        async def respond(request):
            requests.append(request)
            if len(requests) == 1:
                raise error("fixture", request=request)
            return httpx.Response(200, json={"ret": 0})

        client = IlinkClient(transport=httpx.MockTransport(respond))
        try:
            channel = WeixinAdapter(WeixinConfig(enabled=True), AsyncMock(), store=WeixinStore(tmp_path / "weixin"),
                                    client=client)
            channel._account = WeixinAccount("account", "test-token", "https://fixture.invalid", OWNER)
            channel._state = "online"
            now = at("2026-11-18T12:01")
            check(store, tmp_path, next_at=now, targets=["weixin"])
            run = finish(store, now, output="delivery under test")
            recorder = AsyncMock()
            push = weixin_job_push(SimpleNamespace(adapter=lambda _: channel),
                                   SimpleNamespace(record_outbound=recorder))
            await JobDelivery(store, desktop=None, weixin=push).deliver_due(now=now)
            reopened = JobStore(store.home)
            await JobDelivery(reopened, desktop=None, weixin=push).deliver_due(now=now + RETRY_SECONDS[0])
            row = next(item for item in reopened.pushes(run["id"]) if item["target"] == "weixin")
            assert row["state"] == ("sent" if safe_to_retry else "failed")
            assert len(requests) == (2 if safe_to_retry else 1)
            assert recorder.await_count == int(safe_to_retry)
        finally:
            await client.close()

    asyncio.run(scenario())
