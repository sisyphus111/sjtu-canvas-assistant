import json
from urllib.parse import parse_qs

import httpx

from sjtu_canvas_assistant.models import VideoSubtitleItem
from sjtu_canvas_assistant.video_client import (
    SJTUVideoClient,
    VIDEO_INFO_URL,
    VIDEO_LIST_URL,
    VIDEO_LTI_AUTH_ACTION,
    VIDEO_LTI_FORM_ACTION,
    VIDEO_TOKEN_BY_ID_URL,
    _format_srt_ms,
    redact_video_payload,
    subtitle_items_to_srt,
)


def test_video_lti_auth_and_list_uses_sessionless_launch() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/courses/42/external_tools/sessionless_launch":
            assert request.headers["authorization"] == "Bearer canvas-token"
            query = parse_qs(str(request.url.query, "utf-8"))
            assert query["id"] == ["8329"]
            assert query["launch_type"] == ["course_navigation"]
            return httpx.Response(
                200,
                json={
                    "url": (
                        "https://oc.sjtu.edu.cn/courses/42/external_tools/8329"
                        "?session_token=secret-session"
                    )
                },
            )
        if request.url.path == "/courses/42/external_tools/8329":
            return httpx.Response(
                200,
                text="""
                <form action="https://v.sjtu.edu.cn/jy-application-canvas-sjtu/oidc/login_initiations">
                  <input name="iss" value="canvas" />
                  <input name="login_hint" value="42" />
                </form>
                """,
            )
        if str(request.url) == VIDEO_LTI_FORM_ACTION:
            return httpx.Response(
                200,
                text=f"""
                <form action="{VIDEO_LTI_AUTH_ACTION}">
                  <input name="state" value="state-1" />
                  <input name="id_token" value="id-token" />
                </form>
                """,
            )
        if str(request.url) == VIDEO_LTI_AUTH_ACTION:
            return httpx.Response(
                302,
                headers={"location": "https://v.sjtu.edu.cn/#/ivsModules/index?tokenId=token-id"},
            )
        if str(request.url).startswith(f"{VIDEO_TOKEN_BY_ID_URL}?"):
            query = parse_qs(str(request.url.query, "utf-8"))
            assert query["tokenId"] == ["token-id"]
            return httpx.Response(
                200,
                json={"code": 200, "data": {"token": "video-token", "params": {"courId": "COUR-1"}}},
            )
        if str(request.url) == VIDEO_LIST_URL:
            assert request.headers["token"] == "video-token"
            assert json.loads(request.content)["canvasCourseId"] == "COUR-1"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "records": [
                            {
                                "videoId": "video-1",
                                "videoName": "第一讲",
                                "userName": "Teacher",
                                "classroomName": "Room",
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, follow_redirects=True)
    client = SJTUVideoClient(
        "https://oc.sjtu.edu.cn",
        "canvas-token",
        http_client=http_client,
    )

    videos = client.list_course_videos(42)

    assert videos[0].video_id == "video-1"
    assert client.current_auth().redacted() == {
        "video_token": "<redacted>",
        "video_course_id": "COUR-1",
        "source": "canvas_sessionless_lti",
    }
    assert len(requests) == 6


def test_video_info_uses_existing_video_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == VIDEO_INFO_URL:
            assert request.headers["token"] == "video-token"
            assert request.content == b"playTypeHls=true&id=video-1&isAudit=true"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "id": "detail-1",
                        "courId": "COUR-1",
                        "videoId": "video-1",
                        "videoName": "第一讲",
                        "videoPlayResponseVoList": [{"playUrl": "https://example.test/video.m3u8"}],
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = SJTUVideoClient(
        "https://oc.sjtu.edu.cn",
        "canvas-token",
        video_token="video-token",
        video_course_id="COUR-1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    info = client.get_video_info(None, "video-1")

    assert info.video_id == "video-1"
    assert info.cour_id == "COUR-1"
    assert len(info.play_items) == 1


def test_subtitle_items_to_srt_uses_next_subtitle_start_as_end_time() -> None:
    srt = subtitle_items_to_srt(
        [
            VideoSubtitleItem(bg=0, ed=1000, res="第一句"),
            VideoSubtitleItem(bg=1500, ed=3000, res="第二句"),
        ]
    )

    assert "00:00:00,000 --> 00:00:01,500" in srt
    assert "00:00:01,500 --> 00:00:03,000" in srt
    assert "第一句" in srt
    assert "第二句" in srt


def test_format_srt_ms() -> None:
    assert _format_srt_ms(3_723_456) == "01:02:03,456"


def test_redact_video_payload_removes_sensitive_values() -> None:
    payload = {
        "token": "secret-token",
        "playUrl": "https://example.test/a.m3u8?tokenId=abc&session_token=def&x=1",
        "pptImgUrl": "https://example.test/p.png?auth_key=ghi&x=1",
        "nested": [{"cookie": "secret-cookie"}],
    }

    redacted = json.dumps(redact_video_payload(payload), ensure_ascii=False)

    assert "secret-token" not in redacted
    assert "abc" not in redacted
    assert "def" not in redacted
    assert "ghi" not in redacted
    assert "secret-cookie" not in redacted
    assert "<redacted-url>" in redacted
    assert "<redacted>" in redacted
