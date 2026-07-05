from pathlib import Path

import httpx
import pytest

from sjtu_canvas_assistant.canvas_client import CanvasClient
from sjtu_canvas_assistant.exceptions import CanvasAuthError, CanvasNetworkError
from sjtu_canvas_assistant.models import CanvasFile, SubmissionAttachment


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


def test_get_my_submission_uses_self_and_submission_history_include() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/courses/42/assignments/7/submissions/self"
        includes = request.url.params.get_list("include[]")
        assert "submission_history" in includes
        return httpx.Response(
            200,
            json={
                "id": 99,
                "assignment_id": 7,
                "workflow_state": "submitted",
                "attachments": [{"id": 11, "display_name": "answer.pdf", "size": 3}],
            },
        )

    client = CanvasClient(
        "https://oc.sjtu.edu.cn",
        "secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    submission = client.get_my_submission(42, 7)

    assert submission.id == 99
    assert submission.attachments[0].effective_name == "answer.pdf"


def test_get_my_submission_accepts_canvas_null_string_fields() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 99,
                "assignment_id": 7,
                "workflow_state": None,
                "submission_type": None,
                "preview_url": None,
                "attachments": [
                    {
                        "id": 11,
                        "display_name": None,
                        "filename": "answer.pdf",
                        "url": None,
                        "content-type": None,
                    }
                ],
                "submission_history": [
                    {
                        "id": 98,
                        "attempt": 1,
                        "workflow_state": None,
                        "submission_type": None,
                        "attachments": [],
                    }
                ],
            },
        )

    client = CanvasClient(
        "https://oc.sjtu.edu.cn",
        "secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    submission = client.get_my_submission(42, 7)

    assert submission.workflow_state == ""
    assert submission.submission_type == ""
    assert submission.preview_url == ""
    assert submission.attachments[0].display_name == ""
    assert submission.attachments[0].effective_name == "answer.pdf"
    assert submission.attachments[0].url == ""
    assert submission.attachments[0].content_type == ""
    assert submission.submission_history[0].workflow_state == ""
    assert submission.submission_history[0].submission_type == ""


def test_download_submission_attachment_uses_public_url_when_attachment_url_missing(tmp_path: Path) -> None:
    payload = b"abc"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/files/11/public_url":
            assert request.url.params["submission_id"] == "99"
            return httpx.Response(
                200,
                json={"public_url": "https://oc.sjtu.edu.cn/files/11/download?verifier=secret"},
            )
        if request.url.path == "/files/11/download":
            assert request.url.params["verifier"] == "secret"
            return httpx.Response(200, content=payload)
        raise AssertionError(f"unexpected request: {request.url}")

    client = CanvasClient(
        "https://oc.sjtu.edu.cn",
        "secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    attachment = SubmissionAttachment(id=11, display_name="answer.pdf", size=3)
    target = tmp_path / "answer.pdf"

    client.download_submission_attachment(attachment, target, submission_id=99)

    assert target.read_bytes() == payload


def test_network_error_does_not_expose_verifier_query() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = CanvasClient(
        "https://oc.sjtu.edu.cn",
        "secret-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(CanvasNetworkError) as exc:
        client._request("GET", "https://oc.sjtu.edu.cn/files/11/download?verifier=secret")

    assert "verifier" not in str(exc.value)
    assert "secret" not in str(exc.value)
