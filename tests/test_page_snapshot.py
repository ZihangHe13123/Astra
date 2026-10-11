import asyncio
from pathlib import Path

import pytest

from agent.runtime.page_snapshot import PageSnapshot, PageSnapshotBroker, PageSnapshotError, is_page_path


def run(coro):
    return asyncio.run(coro)


async def _answer(broker, sent, **answer):
    """Reply to the one request the broker has sent, as a client would."""
    while not sent:
        await asyncio.sleep(0)
    return broker.resolve({"type": "page_snapshot_response", "request_id": sent[-1]["request_id"], **answer})


def test_a_request_names_the_page_and_returns_the_picture_the_client_saved(tmp_path):
    sent = []
    broker = PageSnapshotBroker(sent.append)
    page = tmp_path / "report.page.html"

    async def scenario():
        waiting = asyncio.ensure_future(broker.request(page))
        accepted = await _answer(
            broker, sent, image_path=str(tmp_path / "report.png"),
            width=1000, height=2100, page_height=5400, script_error="  chart is not defined  ",
        )
        return accepted, await waiting

    accepted, picture = run(scenario())

    assert accepted is True
    assert picture == PageSnapshot(
        image_path=tmp_path / "report.png", height=2100, page_height=5400, script_error="chart is not defined",
    )
    assert [(event["type"], event["path"]) for event in sent] == [("page_snapshot_request", str(page))]
    assert sent[0]["request_id"]


def test_each_answer_settles_only_the_request_it_names(tmp_path):
    sent = []
    broker = PageSnapshotBroker(sent.append)

    async def scenario():
        first = asyncio.ensure_future(broker.request(tmp_path / "a.html"))
        second = asyncio.ensure_future(broker.request(tmp_path / "b.html"))
        while len(sent) < 2:
            await asyncio.sleep(0)
        by_page = {Path(event["path"]).name: event["request_id"] for event in sent}
        broker.resolve({"request_id": by_page["b.html"], "image_path": "b.png"})
        broker.resolve({"request_id": by_page["a.html"], "image_path": "a.png"})
        return await first, await second

    first, second = run(scenario())

    assert (first.image_path, second.image_path) == (Path("a.png"), Path("b.png"))


def test_sizes_the_client_states_badly_are_left_unstated(tmp_path):
    sent = []
    broker = PageSnapshotBroker(sent.append)

    async def scenario():
        waiting = asyncio.ensure_future(broker.request(tmp_path / "report.html"))
        await _answer(broker, sent, image_path="report.png", height="tall", page_height=-5, script_error=7)
        return await waiting

    assert run(scenario()) == PageSnapshot(image_path=Path("report.png"))


def test_a_client_error_reaches_the_caller_in_the_client_s_words(tmp_path):
    sent = []
    broker = PageSnapshotBroker(sent.append)

    async def scenario():
        waiting = asyncio.ensure_future(broker.request(tmp_path / "report.html"))
        await _answer(broker, sent, error="The page is outside this conversation's folders.")
        await waiting

    with pytest.raises(PageSnapshotError, match="outside this conversation"):
        run(scenario())


def test_an_answer_with_neither_picture_nor_error_is_a_failure(tmp_path):
    sent = []
    broker = PageSnapshotBroker(sent.append)

    async def scenario():
        waiting = asyncio.ensure_future(broker.request(tmp_path / "report.html"))
        await _answer(broker, sent)
        await waiting

    with pytest.raises(PageSnapshotError, match="no picture"):
        run(scenario())


def test_a_client_that_never_answers_is_given_up_on_and_its_late_answer_ignored(tmp_path):
    sent = []
    broker = PageSnapshotBroker(sent.append, timeout=0.01)

    async def scenario():
        with pytest.raises(PageSnapshotError, match="in time"):
            await broker.request(tmp_path / "report.html")
        return broker.resolve({"request_id": sent[0]["request_id"], "image_path": "late.png"})

    assert run(scenario()) is False


def test_an_answer_nobody_asked_for_is_ignored():
    broker = PageSnapshotBroker(lambda event: None)

    assert broker.resolve({"request_id": "unknown", "image_path": "x.png"}) is False
    assert broker.resolve({"image_path": "x.png"}) is False


def test_a_page_is_recognised_by_its_extension_alone():
    assert is_page_path("report.page.html")
    assert is_page_path(' "C:\\work\\Report.HTML" ')
    assert is_page_path("old.htm")
    assert not is_page_path("report.html.png")
    assert not is_page_path("photo.png")
