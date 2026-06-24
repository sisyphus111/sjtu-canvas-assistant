import json

from sjtu_canvas_assistant.cli import _format_bytes, _print_json
from sjtu_canvas_assistant.models import User


def test_print_json_writes_machine_readable_stdout(capsys) -> None:
    user = User(
        id=1,
        name="贺昺",
        avatar_url="https://oc.sjtu.edu.cn/images/thumbnails/example/very-long-url",
    )

    _print_json(user)

    captured = capsys.readouterr()
    assert json.loads(captured.out)["name"] == "贺昺"
    assert captured.err == ""


def test_format_bytes() -> None:
    assert _format_bytes(0) == "0 B"
    assert _format_bytes(1023) == "1023 B"
    assert _format_bytes(1024) == "1.0 KiB"
    assert _format_bytes(1024 * 1024) == "1.0 MiB"
