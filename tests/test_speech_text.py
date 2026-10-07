"""What a streamed reply turns into before it is spoken."""
from agent.speech.text import SpeechSegmenter, speakable


def units(*chunks: str, **options) -> list[str]:
    segmenter = SpeechSegmenter(**options)
    spoken = [unit for chunk in chunks for unit in segmenter.feed(chunk)]
    return spoken + segmenter.flush()


def test_first_clause_is_released_before_the_sentence_ends():
    segmenter = SpeechSegmenter()
    assert segmenter.feed("你回来啦，今天的问题我已经看过一遍了") == ["你回来啦，"]
    assert segmenter.feed("，不算难。我们一步一步来，好") == ["今天的问题我已经看过一遍了，不算难。"]
    assert segmenter.feed("吗？然后") == ["我们一步一步来，好吗？"]
    assert segmenter.flush() == ["然后"]


def test_units_do_not_depend_on_how_the_stream_was_chunked():
    reply = "修好了！这次一次就过了，太好了！\n\n接下来我把 fix 推到 main，然后你 pull 一下，再跑一遍 test 就行。"
    whole = units(reply)
    assert whole == units(*reply)  # one character at a time
    assert whole == units(reply[:7], reply[7:23], reply[23:])
    assert "".join(whole).replace(" ", "") == reply.replace("\n", "").replace(" ", "")


def test_ascii_marks_end_a_clause_only_before_a_space():
    assert units("Version 3.14 of main.py is out. Run it, then check http://localhost:8000/v1/models?x=1 again.") == [
        "Version 3.14 of main.py is out.", "Run it, then check again."]


def test_code_tables_rules_and_urls_are_not_read():
    reply = (
        "先看结论：粘贴时多了一层标记。\n\n"
        "```python\nprint('never spoken')\n```\n\n"
        "| 列 | 值 |\n| --- | --- |\n| a | 1 |\n\n"
        "---\n"
        "- 详见 [官方文档](https://example.com/docs?a=1,b=2)，里面有说明。\n"
        "- 入口在 `ui-tui/src/index.tsx`，函数叫 `submit`。\n"
    )
    assert units(reply) == ["先看结论：", "粘贴时多了一层标记。", "详见 官方文档，里面有说明。", "入口在 ，函数叫 submit。"]


def test_a_code_fence_split_across_chunks_stays_hidden():
    assert units("好的。\n``", "`sh\nrm -rf build\n`", "``\n完成了。") == ["好的。", "完成了。"]


def test_markup_emoji_and_kaomoji_are_dropped_but_asides_are_kept():
    assert units("## **修好了**！喵——！ (=^･ω･^=) ✨（笑）(see below)") == ["修好了！", "喵——！ （笑）(see below)"]
    assert speakable("…… 你也还没睡呀。") == "你也还没睡呀。"
    assert speakable("(=^･ω･^=)") == ""


def test_a_line_with_nothing_to_say_produces_no_unit():
    assert units("✨✨✨\n(｡•̀ᴗ-)✧\n嗯。") == ["嗯。"]


def test_a_long_run_without_marks_is_still_cut():
    spoken = units("这" * 200, max_chars=40)
    assert [len(unit) for unit in spoken] == [40, 40, 40, 40, 40]
