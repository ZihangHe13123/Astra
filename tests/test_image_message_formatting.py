from agent.cli.images import build_user_message_content


def test_image_attachment_removes_only_path_spans_and_preserves_message_format(tmp_path):
    image = tmp_path / 'sample image.png'
    image.write_bytes(b'png fixture')
    prefix = '  请检查：\r\n\r\n```python\r\nif ready:\r\n    print("a  b")\r\n```\r\n'
    suffix = '\r\n\r\n- 第一项\r\n\t- 子项  \r\n'
    blocks = build_user_message_content(prefix + f'"{image}"' + suffix)
    assert blocks[0].data['text'] == prefix + suffix
    assert [block.type for block in blocks] == ['text', 'image_url']


def test_image_command_keeps_multiline_prompt_and_existing_command_behavior(tmp_path):
    first, second = tmp_path / 'first.png', tmp_path / 'second.jpg'
    first.write_bytes(b'png fixture')
    second.write_bytes(b'jpg fixture')
    prompt = '说明两张图片：\n\n```python\nif ready:\n    print("a  b")\n```'
    blocks = build_user_message_content(f'/image "{first}" "{second}" {prompt}')
    assert blocks[0].data['text'] == prompt
    assert [block.type for block in blocks] == ['text', 'image_url', 'image_url']


def test_ordinary_attachment_preserves_first_line_indentation_and_trailing_newlines(tmp_path):
    image = tmp_path / 'sample.png'
    image.write_bytes(b'png fixture')
    prompt = '    indented block\n        nested line\n\n'
    blocks = build_user_message_content(prompt + f'"{image}"')
    assert blocks[0].data['text'] == prompt
