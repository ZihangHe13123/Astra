import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_jobs_delivery import at, check, finish
from test_weixin_channel import adapter, logged_in, until
from weixin_fixture import OWNER, FakeIlink
from agent.channels.job_push import weixin_job_push
from agent.channels.weixin import WeixinStore
from agent.runtime.jobs.delivery import JobDelivery, RETRY_SECONDS
from agent.runtime.jobs.store import JobStore


def test_retry_never_resends_an_already_acknowledged_wechat_piece(tmp_path):
    async def scenario():
        server = FakeIlink()
        channel = None
        try:
            config_store = WeixinStore(tmp_path / "test-login")
            await logged_in(server, config_store)
            channel = adapter(server, config_store, AsyncMock(), max_output_chars=200)
            await channel.start()
            await until(lambda: channel.running, "local fixture online")
            store = JobStore(tmp_path / "job-state")
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
