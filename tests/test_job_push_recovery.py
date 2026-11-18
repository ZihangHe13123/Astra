import asyncio
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
