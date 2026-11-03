import json
from urllib.parse import parse_qs

import httpx
import pytest

from sjtu_canvas_assistant.exceptions import VideoAuthError, VideoIntegrationError
from sjtu_canvas_assistant.models import VideoSubtitleItem
from sjtu_canvas_assistant.video_client import (
    SJTUVideoClient,
    VIDEO_CONTEXT_URL,
    VIDEO_INFO_URL,
    VIDEO_LIST_URL,
    VIDEO_LTI_AUTH_ACTION,
    VIDEO_LTI_FORM_ACTION,
    PPT_SLICE_URL,
    SUBTITLE_URL,
    _format_srt_ms,
    redact_video_payload,
    subtitle_items_to_srt,
)


def test_new_lti_launch_context_and_paginated_recordings() -> None:
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/courses/42/external_tools/sessionless_launch":
            assert request.headers["authorization"] == "Bearer canvas-token"
            assert request.url.params["id"] == "8329"
            return httpx.Response(200, json={"url": "https://oc.sjtu.edu.cn/launch?session_token=secret"})
        # The Canvas token is only attached to its API, never posted to LTI.
        assert "authorization" not in request.headers
        if request.url.path == "/launch":
            return httpx.Response(200, headers={"set-cookie": "canvas_session=one; Path=/"}, text=f'''
                <form action="{VIDEO_LTI_FORM_ACTION}">
                    <input name="login_hint" value="hint" />
                </form>''')
        if str(request.url) == VIDEO_LTI_FORM_ACTION:
            assert request.method == "POST"
            assert parse_qs(request.content.decode())["login_hint"] == ["hint"]
            return httpx.Response(302, headers={"location": "https://oc.sjtu.edu.cn/authorize"})
        if request.url.path == "/authorize":
            assert "canvas_session=one" in request.headers["cookie"]
            return httpx.Response(200, text=f'''
                <form action="{VIDEO_LTI_AUTH_ACTION}">
                    <input name="id_token" value="id-token" />
                </form>''')
        if str(request.url) == VIDEO_LTI_AUTH_ACTION:
            assert request.method == "POST"
            assert parse_qs(request.content.decode())["id_token"] == ["id-token"]
            return httpx.Response(302, headers={"location": "https://v.sjtu.edu.cn/ui/#/lms/launch?jwt_token=video-jwt"})
        if request.url.path == "/ui/":
            return httpx.Response(200, text="app")
        assert request.headers["jwt-token"] == "video-jwt"
        if str(request.url) == VIDEO_CONTEXT_URL:
            return httpx.Response(200, json={"status": 200, "ok": True, "data": {"canvasRecord": {"teachingClassId": 77}}})
        if request.url.path == httpx.URL(VIDEO_LIST_URL).path:
            assert request.method == "GET"
            assert request.url.params["teclIds"] == "77"
            assert request.url.params["page.orders[0].field"] == "courBeginTime"
            page = int(request.url.params["page.pageIndex"])
            pages.append(page)
            return httpx.Response(200, json={"status": 200, "data": {"pageCount": 2, "records": [{
                "id": page, "subjName": "Example course", "teacNames": "Example teacher",
                "clroName": "Example room", "courBeginTime": f"2026-01-0{page} 10:00:00",
                "vodDisplayStatus": 5 if page == 1 else 2, "vodClickEnable": 1,
            }]}})
        raise AssertionError(f"unexpected path: {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        client = SJTUVideoClient("https://oc.sjtu.edu.cn", "canvas-token", http_client=http)
        videos = client.list_course_videos(42)
    assert pages == [1, 2]
    assert [v.video_id for v in videos] == ["1", "2"]
    assert [v.playable for v in videos] == [True, False]
    assert videos[0].course_begin_time == "2026-01-01 10:00:00"
    assert videos[0].classroom_name == "Example room"
    assert client.current_auth().redacted() == {
        "video_token": "<redacted>", "video_course_id": "77", "source": "canvas_sessionless_lti",
    }


def video_client(handler):
    return SJTUVideoClient(
        "https://oc.sjtu.edu.cn", "canvas-token", video_token="video-jwt", video_course_id="77",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_video_info_uses_recording_id_and_new_channels() -> None:
    def handler(request):
        assert request.method == "GET"
        assert request.url.params["courseId"] == "101"
        assert request.headers["jwt-token"] == "video-jwt"
        return httpx.Response(200, json={"status": 200, "data": {
            "id": 101, "courName": "Example lecture", "classRoomName": "Room",
            "courseVodViewList": [{"courId": 9999, "viewNum": 5, "url": "https://media.test/ppt.mp4?signature=secret"}],
        }})
    info = video_client(handler).get_video_info(None, "101")
    assert info.video_id == "101"
    assert info.video_name == "Example lecture"
    assert len(info.play_items) == 1
    assert "secret" not in json.dumps(redact_video_payload(info))


@pytest.mark.parametrize("asset", ["subtitle", "ppt"])
def test_null_optional_ai_data_is_unavailable(asset) -> None:
    def handler(request):
        assert request.method == "GET"
        if asset == "subtitle":
            assert request.url.path.endswith("/translate/101")
            assert request.url.params["useOriginal"] == "true"
        else:
            assert request.url.params["courseId"] == "101"
        return httpx.Response(200, json={"status": 200, "ok": True, "dataOk": False, "data": None})
    client = video_client(handler)
    if asset == "subtitle":
        body = client.get_video_subtitle(42, "101")
        assert body.availability == "unavailable"
        assert body.preferred_items == []
    else:
        assert client.get_video_ppt_slices(42, "101") == []


def test_nonempty_ai_data_normalizes_subtitles_and_ppt_seconds() -> None:
    def handler(request):
        if request.url.path.startswith(httpx.URL(SUBTITLE_URL).path):
            data = {"beforeAssemblyList": [{"bg": 1250, "ed": 3000, "res": "example"}]}
        else:
            assert request.url.path == httpx.URL(PPT_SLICE_URL).path
            data = {"docList": [{"imageSeekTime": "1.25", "imageUrl": "https://media.test/1.png"}]}
        return httpx.Response(200, json={"status": 200, "data": data})
    client = video_client(handler)
    body = client.get_video_subtitle(42, "101")
    assert body.availability == "available"
    assert "00:00:01,250 --> 00:00:03,000" in subtitle_items_to_srt(body.preferred_items)
    assert client.get_video_ppt_slices(42, "101")[0].create_sec == 1250


@pytest.mark.parametrize("payload,error", [
    ({"status": 401, "message": "expired"}, VideoAuthError),
    ({"status": 403, "message": "denied"}, VideoAuthError),
    ({"status": 500, "message": "failed"}, VideoIntegrationError),
    ({"status": 200, "ok": False}, VideoIntegrationError),
])
def test_http_success_does_not_hide_application_errors(payload, error) -> None:
    client = video_client(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(error):
        client.get_video_subtitle(42, "101")


@pytest.mark.parametrize("method,payload", [
    ("list", {"data": {}}),
    ("subtitle", {"data": {"unexpected": []}}),
    ("ppt", {"data": {"unexpected": []}}),
])
def test_changed_response_shape_is_not_silently_treated_as_no_assets(method, payload) -> None:
    client = video_client(lambda _: httpx.Response(200, json={"status": 200, **payload}))
    with pytest.raises(VideoIntegrationError):
        if method == "list":
            client.list_course_videos(42)
        elif method == "subtitle":
            client.get_video_subtitle(42, "101")
        else:
            client.get_video_ppt_slices(42, "101")


def test_reusing_client_for_another_canvas_course_relaunches_auth(monkeypatch) -> None:
    client = video_client(lambda _: pytest.fail("unexpected network call"))
    client._canvas_course_id = 42
    calls = []
    def launch(course_id):
        calls.append(course_id)
        raise RuntimeError("relaunch requested")
    monkeypatch.setattr(client, "get_sessionless_launch_url", launch)
    with pytest.raises(RuntimeError, match="relaunch requested"):
        client._ensure_auth(43)
    assert calls == [43]


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


@pytest.mark.parametrize("payload", [None, {}, {"status": 200}])
def test_invalid_envelope_does_not_masquerade_as_missing_ai(payload):
    client = video_client(lambda _: httpx.Response(200, json=payload) if payload is not None else httpx.Response(200, content=b"null"))
    with pytest.raises(VideoIntegrationError):
        client.get_video_subtitle(42, "101")


def test_new_jwt_and_watching_keys_are_redacted():
    raw = {
        "jwt-token": "private-header",
        "liveWatchingKey": "private-watch-key",
        "message": "redirect https://example.test/#/launch?jwt_token=private-jwt&id_token=private-id",
    }
    output = json.dumps(redact_video_payload(raw))
    for secret in ("private-header", "private-watch-key", "private-jwt", "private-id"):
        assert secret not in output
