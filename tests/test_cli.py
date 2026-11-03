import json

import pytest

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


@pytest.mark.parametrize("preexisting", [False, True])
def test_unavailable_subtitle_does_not_create_or_truncate_srt(tmp_path, monkeypatch, preexisting):
    from typer.testing import CliRunner
    import sjtu_canvas_assistant.cli as cli
    from sjtu_canvas_assistant.models import VideoSubtitleBody

    class VideoClient:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get_video_subtitle(self, course_id, video_id):
            return VideoSubtitleBody(availability="unavailable")

    monkeypatch.setattr(cli, "_make_video_client", lambda *args: (VideoClient(), tmp_path))
    target = tmp_path / "transcript.srt"
    if preexisting:
        target.write_text("existing transcript", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["video", "subtitle", "42", "101", "--output", str(target), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["availability"] == "unavailable"
    assert payload["srt_path"] is None
    if preexisting:
        assert target.read_text() == "existing transcript"
    else:
        assert not target.exists()
