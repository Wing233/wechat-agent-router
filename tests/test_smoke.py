from attachments import Attachments
from hub import parse_attachments, parse_quote, parse_text
from imagegen import ImageGen
from registry import ToolRegistry


class DummyHub:
    pass


class DummyLLM:
    pass


def test_parse_text_and_quote():
    message = {
        "item_list": [
            {
                "type": "text",
                "text": "同意",
                "ref_msg": {"title": "微桥", "item": {"type": "text", "text": "回复编号：ABCD"}},
            }
        ]
    }
    assert parse_text(message) == "同意"
    assert parse_quote(message) == {"title": "微桥", "type": "text", "text": "回复编号：ABCD"}


def test_parse_attachments():
    message = {
        "item_list": [
            {"type": "image", "media": {"encrypt_query_param": "eqp", "aes_key": "aes"}},
            {"type": "file", "file_name": "notes.pdf", "media": {"file_size": 12}},
            {"type": "text", "text": "hello"},
        ]
    }
    items = parse_attachments(message)
    assert [item["type"] for item in items] == ["image", "file"]
    assert items[0]["encrypt_query_param"] == "eqp"
    assert items[1]["file_name"] == "notes.pdf"


def test_extract_text(tmp_path):
    service = Attachments(DummyHub(), DummyLLM(), base_dir=tmp_path)
    text, mode = service._extract_text("你好，微桥".encode("utf-8"), "hello.txt")
    assert text == "你好，微桥"
    assert mode in {"utf-8", "utf-8-sig"}


def test_tool_registry_alias():
    registry = ToolRegistry()
    registry.register("ping", "ping", {"type": "object"}, lambda _: {"ok": True},
                      aliases=("pong",))
    assert {item["function"]["name"] for item in registry.definitions()} == {"ping"}
    assert registry.execute("pong", {}) == {"ok": True}


def test_imagegen_disabled():
    generator = ImageGen("https://example.com/v1", "", "model", enabled=False)
    assert generator.enabled is False
