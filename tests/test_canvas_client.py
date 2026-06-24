from pathlib import Path

import httpx
import pytest

from sjtu_canvas_assistant.canvas_client import CanvasClient
from sjtu_canvas_assistant.exceptions import CanvasAuthError
from sjtu_canvas_assistant.models import CanvasFile


def test_list_courses_uses_bearer_token_and_canvas_pagination() -> None:
    seen_auth_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth_headers.append(request.headers.get("authorization"))
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "A"}],
                headers={
                    "Link": '<https://oc.sjtu.edu.cn/api/v1/courses?page=2&per_page=100>; rel="next"'
                },
            )
        return httpx.Response(200, json=[{"id": 2, "name": "B"}])

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, follow_redirects=True)
    client = CanvasClient("https://oc.sjtu.edu.cn", "secret-token", http_client=http_client)

    courses = client.list_courses()

    assert [course.id for course in courses] == [1, 2]
    assert seen_auth_headers == ["Bearer secret-token", "Bearer secret-token"]


def test_unauthorized_response_becomes_token_safe_auth_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad token"})

    client = CanvasClient(
        "https://oc.sjtu.edu.cn",
        "secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(CanvasAuthError) as exc:
        client.get_current_user()

    assert "secret-token" not in str(exc.value)
    assert "CANVAS_TOKEN" in str(exc.value)


def test_download_file_streams_to_destination(tmp_path: Path) -> None:
    payload = b"hello canvas"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://oc.sjtu.edu.cn/files/7/download"
        return httpx.Response(200, content=payload)

    client = CanvasClient(
        "https://oc.sjtu.edu.cn",
        "secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    canvas_file = CanvasFile(
        id=7,
        display_name="hello.txt",
        url="https://oc.sjtu.edu.cn/files/7/download",
        size=len(payload),
    )
    target = tmp_path / "hello.txt"

    client.download_file(canvas_file, target)

    assert target.read_bytes() == payload
