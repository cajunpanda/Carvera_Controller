"""Tests for the Remote API console buffer."""

from carveracontroller.addons.remote_api.console import ConsoleBuffer


def test_since_returns_newer_lines_in_order():
    buf = ConsoleBuffer()
    for text in ["a", "b", "c"]:
        buf.append("output", text, timestamp=1.0)
    result = buf.since(1, 10)
    assert [line["text"] for line in result["lines"]] == ["b", "c"]
    assert [line["seq"] for line in result["lines"]] == [2, 3]
    assert result["latest"] == 3
    assert result["missed"] is False


def test_limit_returns_oldest_first():
    buf = ConsoleBuffer()
    for i in range(5):
        buf.append("output", str(i))
    assert [line["text"] for line in buf.since(0, 2)["lines"]] == ["0", "1"]


def test_missed_when_buffer_wrapped():
    buf = ConsoleBuffer(maxlen=3)
    for i in range(6):
        buf.append("error", str(i))
    result = buf.since(0, 10)
    assert [line["seq"] for line in result["lines"]] == [4, 5, 6]
    assert result["missed"] is True
    assert buf.since(3, 10)["missed"] is False


def test_empty_buffer():
    result = ConsoleBuffer().since(0, 10)
    assert result == {"lines": [], "latest": 0, "missed": False}
