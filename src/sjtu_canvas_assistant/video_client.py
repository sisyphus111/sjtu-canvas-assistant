"""SJTU Canvas video/LTI client.

The public CLI still authenticates with Canvas Access Token only. For video
data, Canvas first creates a sessionless LTI launch URL. The launch exchanges
through SJTU's video service and yields a short-lived video API token.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError

from .exceptions import VideoAuthError, VideoIntegrationError, VideoNetworkError
from .models import (
    CanvasVideo,
    VideoAuth,
    VideoInfo,
    VideoPPTSlice,
    VideoSubtitleBody,
    VideoSubtitleItem,
)

VIDEO_SERVICE_ROOT = "https://v.sjtu.edu.cn/jy-application-resourcemanage"
VIDEO_UI_REFERER = "https://v.sjtu.edu.cn/jy-application-resourcemanage-ui/"
VIDEO_LTI_TOOL_ID = 8329
VIDEO_LTI_FORM_ACTION = "https://v.sjtu.edu.cn/jy-lti-adapter/lti/canvas/oidc/login-initiation/canvas-record"
VIDEO_LTI_AUTH_ACTION = "https://v.sjtu.edu.cn/jy-lti-adapter/lti/canvas/launch/canvas-record"
VIDEO_CONTEXT_URL = f"{VIDEO_SERVICE_ROOT}/lms/launch-context"
VIDEO_LIST_URL = f"{VIDEO_SERVICE_ROOT}/v1/subject_vod_list_new"
VIDEO_INFO_URL = f"{VIDEO_SERVICE_ROOT}/v1/course_vod_urls_new"
SUBTITLE_URL = f"{VIDEO_SERVICE_ROOT}/v1/course/ai/translate"
PPT_SLICE_URL = f"{VIDEO_SERVICE_ROOT}/v1/course/ai/ppt"

_SUCCESS_CODES = {"0", "200", "success", "true", "ok"}


@dataclass(frozen=True)
class FormPost:
    action: str
    data: Dict[str, str]


def _redact_sensitive_text(text: str) -> str:
    patterns = [
        (r"(session_token=)[^&\s\"']+", r"\1<redacted>"),
        (r"(tokenId=)[^&\s\"']+", r"\1<redacted>"),
        (r"([?&](?:token|jwt_token|id_token)=)[^&\s\"']+", r"\1<redacted>"),
        (
            r"([?&](?:access_token|auth_key|sign|signature|secret|key|expires|policy|wsSecret|wsTime|X-Amz-[^=]+)=)[^&\s\"']+",
            r"\1<redacted>",
        ),
        (r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>"),
        (r"(\"(?:token|jwt_token|id_token)\"\s*:\s*\")[^\"]+", r"\1<redacted>"),
    ]
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    return redacted


def redact_video_payload(value: Any) -> Any:
    """Return a token-safe representation of video payloads for CLI/MCP output."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[attr-defined]
    if isinstance(value, Mapping):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            if normalized_key.endswith("key") or re.search(r"(token|cookie|authorization|secret|session)", key_text, re.IGNORECASE):
                redacted[key_text] = "<redacted>" if item else item
            elif "url" in normalized_key and normalized_key not in {
                "pptimgurl",
                "pptimageurl",
                "imgurl",
                "imageurl",
            }:
                redacted[key_text] = "<redacted-url>" if item else item
            else:
                redacted[key_text] = redact_video_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_video_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _safe_url_label(url: object) -> str:
    parsed = urlparse(str(url))
    if not parsed.scheme or not parsed.netloc:
        return str(url).split("?", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _format_srt_ms(milliseconds: int) -> str:
    total = max(0, int(milliseconds))
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def subtitle_items_to_srt(items: Iterable[VideoSubtitleItem]) -> str:
    """Convert SJTU subtitle rows into SRT text."""

    usable = [item for item in items if (item.res or "").strip()]
    lines: List[str] = []
    for index, item in enumerate(usable, start=1):
        next_item = usable[index] if index < len(usable) else None
        start_ms = int(item.bg or 0)
        end_ms = int(next_item.bg) if next_item is not None else int(item.ed or 0)
        if end_ms <= start_ms:
            end_ms = max(int(item.ed or 0), start_ms + 1_000)
        text = "\n".join(line.strip() for line in item.res.splitlines() if line.strip())
        lines.extend(
            [
                str(index),
                f"{_format_srt_ms(start_ms)} --> {_format_srt_ms(end_ms)}",
                text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


class SJTUVideoClient:
    """Client for SJTU's Canvas video integration."""

    def __init__(
        self,
        canvas_base_url: str,
        canvas_token: str,
        *,
        timeout_seconds: float = 30.0,
        video_token: str = "",
        video_course_id: str = "",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.canvas_base_url = canvas_base_url.rstrip("/")
        self.canvas_token = canvas_token
        self.video_token = video_token.strip()
        self.video_course_id = video_course_id.strip()
        self._canvas_course_id: Optional[int] = None
        self._auth_source = "environment" if self.video_token and self.video_course_id else ""
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={
                "User-Agent": "sjtu-canvas-assistant/0.1",
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SJTUVideoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _canvas_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.canvas_base_url}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        data: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        follow_redirects: Optional[bool] = None,
        service: str = "video",
    ) -> httpx.Response:
        kwargs: Dict[str, Any] = {
            "params": params,
            "headers": headers,
            "data": data,
            "json": json_body,
        }
        if follow_redirects is not None:
            kwargs["follow_redirects"] = follow_redirects
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise VideoNetworkError(
                f"{service} request timed out: {method} {_safe_url_label(url)}"
            ) from exc
        except httpx.TransportError as exc:
            raise VideoNetworkError(
                f"{service} network failure: {method} {_safe_url_label(url)}"
            ) from exc

        if response.status_code >= 400:
            message = self._safe_error_message(response)
            if response.status_code in {401, 403}:
                raise VideoAuthError(
                    f"{service} denied access ({response.status_code}): {message}"
                )
            raise VideoIntegrationError(
                f"{service} returned HTTP {response.status_code}: {message}"
            )
        return response

    def _canvas_api_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        response = self._request(
            method,
            self._canvas_url(path),
            params=params,
            headers={"Authorization": f"Bearer {self.canvas_token}"},
            service="canvas",
        )
        return self._decode_json(response, "Canvas")

    def _video_api_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        auth_required: bool = True,
    ) -> Any:
        headers = (
            self._video_headers()
            if auth_required
            else {"Accept": "application/json, text/plain, */*", "Referer": VIDEO_UI_REFERER}
        )
        response = self._request(
            method,
            url,
            params=params,
            data=data,
            json_body=json_body,
            headers=headers,
            service="video",
        )
        payload = self._decode_json(response, "SJTU video")
        return self._unwrap_video_payload(payload)

    @staticmethod
    def _decode_json(response: httpx.Response, service_label: str) -> Any:
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise VideoIntegrationError(
                f"{service_label} returned invalid JSON from {_safe_url_label(response.url)}."
            ) from exc

    @staticmethod
    def _safe_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.reason_phrase
        if isinstance(payload, Mapping):
            for key in ("message", "msg", "error", "errors"):
                value = payload.get(key)
                if value:
                    return _redact_sensitive_text(str(value)[:300])
        return response.reason_phrase

    @staticmethod
    def _unwrap_video_payload(payload: Any) -> Any:
        if not isinstance(payload, Mapping):
            raise VideoIntegrationError("SJTU video response did not contain an API envelope.")
        # dataOk=false with data=null means the optional asset is absent, not
        # failed authentication. HTTP-200 error envelopes must still fail.
        code = payload.get("status", payload.get("code"))
        if (
            (code is not None and str(code).lower() not in _SUCCESS_CODES)
            or payload.get("success") is False
            or payload.get("ok") is False
        ):
            message = payload.get("msg") or payload.get("message") or "SJTU video API returned an error."
            error = VideoAuthError if str(code) in {"401", "403"} else VideoIntegrationError
            raise error(_redact_sensitive_text(str(message)))
        if "data" not in payload:
            raise VideoIntegrationError("SJTU video response did not contain an API data field.")
        return payload["data"]

    def _video_headers(self) -> Dict[str, str]:
        if not self.video_token:
            raise VideoAuthError("SJTU video token is missing. Resolve LTI auth first.")
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": VIDEO_UI_REFERER,
            "jwt-token": self.video_token,
        }

    @staticmethod
    def _extract_form(html: str, *, base_url: str, target_action: Optional[str] = None) -> FormPost:
        soup = BeautifulSoup(html, "html.parser")
        forms = list(soup.find_all("form"))
        selected = None
        for form in forms:
            action = str(form.get("action") or "")
            absolute_action = urljoin(base_url, action)
            if target_action is None or urlparse(absolute_action)[:3] == urlparse(target_action)[:3]:
                selected = form
                break
        if selected is None and target_action is None and forms:
            selected = forms[0]
        if selected is None:
            expected = f" for {target_action}" if target_action else ""
            raise VideoAuthError(f"SJTU video LTI page did not contain the expected form{expected}.")

        action = str(selected.get("action") or "")
        if not action:
            raise VideoAuthError("SJTU video LTI form did not include an action URL.")
        data: Dict[str, str] = {}
        for element in selected.find_all(["input", "button"]):
            name = element.get("name")
            if name:
                data[str(name)] = str(element.get("value") or "")
        return FormPost(action=urljoin(base_url, action), data=data)

    @staticmethod
    def _extract_jwt(location: str) -> str:
        parsed = urlparse(location)
        for query in (parsed.query, urlparse(parsed.fragment).query):
            token = parse_qs(query).get("jwt_token")
            if token and token[0].strip():
                return token[0].strip()
        raise VideoAuthError("SJTU video LTI redirect did not include jwt_token.")

    def current_auth(self) -> VideoAuth:
        return VideoAuth(
            video_token=self.video_token,
            video_course_id=self.video_course_id,
            source=self._auth_source,
        )

    def get_sessionless_launch_url(self, course_id: int) -> str:
        payload = self._canvas_api_json(
            "GET",
            f"/api/v1/courses/{course_id}/external_tools/sessionless_launch",
            params={"id": VIDEO_LTI_TOOL_ID, "launch_type": "course_navigation"},
        )
        if not isinstance(payload, Mapping):
            raise VideoAuthError("Canvas sessionless launch returned a non-object payload.")
        launch_url = str(payload.get("url") or "").strip()
        if not launch_url.startswith(("http://", "https://")):
            raise VideoAuthError("Canvas sessionless launch did not return a usable launch URL.")
        return launch_url

    def resolve_course_video_auth(self, course_id: int, *, force: bool = False) -> VideoAuth:
        """Resolve and cache the short-lived SJTU video auth for a Canvas course."""

        if (
            not force and self.video_token and self.video_course_id
            and self._canvas_course_id in {None, course_id}
        ):
            self._canvas_course_id = course_id
            return self.current_auth()

        launch_url = self.get_sessionless_launch_url(course_id)
        response = self._request("GET", launch_url, follow_redirects=True, service="canvas LTI launch")
        # The new adapter redirects through Canvas OIDC authorization between
        # these two forms. Keep the cookie jar in this same HTTP client.
        for action in (VIDEO_LTI_FORM_ACTION, VIDEO_LTI_AUTH_ACTION):
            form = self._extract_form(response.text, base_url=str(response.url), target_action=action)
            response = self._request(
                "POST", form.action, data=form.data,
                headers={"Referer": _safe_url_label(response.url)},
                follow_redirects=True, service="video LTI launch",
            )
        video_token = self._extract_jwt(str(response.url))
        response = self._request("GET", VIDEO_CONTEXT_URL, headers={"jwt-token": video_token}, service="video launch context")
        context = self._unwrap_video_payload(self._decode_json(response, "SJTU video"))
        record = context.get("canvasRecord") if isinstance(context, Mapping) else None
        class_id = record.get("teachingClassId") if isinstance(record, Mapping) else None
        if not class_id:
            raise VideoAuthError("SJTU video launch context did not include canvasRecord.teachingClassId.")
        self.video_token = video_token
        self.video_course_id = str(class_id)
        self._canvas_course_id = course_id
        self._auth_source = "canvas_sessionless_lti"
        return self.current_auth()

    def _ensure_auth(self, course_id: Optional[int]) -> VideoAuth:
        if course_id is not None:
            return self.resolve_course_video_auth(course_id)
        if self.video_token and self.video_course_id:
            return self.current_auth()
        raise VideoAuthError(
            "Canvas course_id is required unless SJTU_VIDEO_TOKEN and SJTU_VIDEO_COURSE_ID are configured."
        )

    def list_course_videos(self, course_id: int) -> List[CanvasVideo]:
        auth = self._ensure_auth(course_id)
        videos: List[CanvasVideo] = []
        page = 1
        while True:
            data = self._video_api_json("GET", VIDEO_LIST_URL, params={
                "page.pageIndex": page, "page.pageSize": 100,
                "teclIds": auth.video_course_id,
                "page.orders[0].asc": "true", "page.orders[0].field": "courBeginTime",
            })
            if not isinstance(data, Mapping) or not isinstance(data.get("records"), list):
                raise VideoIntegrationError("SJTU video list response did not contain a records list.")
            try:
                items = [CanvasVideo.model_validate(item) for item in data["records"]]
                if any(not item.video_id for item in items):
                    raise ValueError("missing video id")
                page_count = int(data["pageCount"])
                if page_count < 0 or (not items and page < page_count):
                    raise ValueError("inconsistent pagination")
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                raise VideoIntegrationError("SJTU video list response shape is not supported yet.") from exc
            videos.extend(items)
            if page >= page_count:
                return videos
            page += 1

    def get_video_info(self, course_id: Optional[int], video_id: str) -> VideoInfo:
        self._ensure_auth(course_id)
        data = self._video_api_json("GET", VIDEO_INFO_URL, params={"courseId": str(video_id)})
        if not isinstance(data, Mapping) or not isinstance(data.get("courseVodViewList"), list):
            raise VideoIntegrationError("SJTU video info response did not contain playback metadata.")
        try:
            return VideoInfo.model_validate({**data, "videoId": str(video_id)})
        except ValidationError as exc:
            raise VideoIntegrationError("SJTU video info response shape is not supported yet.") from exc

    def get_subtitle(self, course_id: Optional[int], video_course_id: str) -> VideoSubtitleBody:
        """Read AI subtitles using the recording id, not a stream's courId."""
        self._ensure_auth(course_id)
        data = self._video_api_json("GET", f"{SUBTITLE_URL}/{video_course_id}", params={"useOriginal": "true"})
        if data is None:
            return VideoSubtitleBody(availability="unavailable")
        if not isinstance(data, Mapping) or not any(k in data for k in ("beforeAssemblyList", "afterAssemblyList")):
            raise VideoIntegrationError("SJTU subtitle response did not contain subtitle rows.")
        try:
            body = VideoSubtitleBody.model_validate(data)
        except ValidationError as exc:
            raise VideoIntegrationError("SJTU subtitle response shape is not supported yet.") from exc
        body.availability = "available" if any(item.res.strip() for item in body.preferred_items) else "unavailable"
        return body

    def get_video_subtitle(self, course_id: int, video_id: str) -> VideoSubtitleBody:
        return self.get_subtitle(course_id, video_id)

    def get_ppt_slices(self, course_id: Optional[int], video_course_id: str) -> List[VideoPPTSlice]:
        self._ensure_auth(course_id)
        data = self._video_api_json("GET", PPT_SLICE_URL, params={"courseId": str(video_course_id)})
        if data is None:
            return []
        if not isinstance(data, Mapping) or not isinstance(data.get("docList"), list):
            raise VideoIntegrationError("SJTU PPT response did not contain a docList.")
        try:
            # imageSeekTime is seconds in the new player; the existing export
            # model stores millisecond offsets despite its create_sec name.
            return [VideoPPTSlice.model_validate({
                "createSec": round(float(item["imageSeekTime"]) * 1000),
                "pptImgUrl": item["imageUrl"], "ocr": item.get("ocr", ""),
            }) for item in data["docList"]]
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            raise VideoIntegrationError("SJTU PPT slice response shape is not supported yet.") from exc

    def get_video_ppt_slices(self, course_id: int, video_id: str) -> List[VideoPPTSlice]:
        return self.get_ppt_slices(course_id, video_id)

    def download_ppt_image(self, image_url: str) -> Tuple[bytes, str]:
        """Download one PPT replay image without exposing signed URL details."""

        if not image_url.startswith(("http://", "https://")):
            raise VideoIntegrationError("PPT image URL is not usable.")
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": VIDEO_UI_REFERER,
        }
        if self.video_token and urlparse(image_url).hostname == "v.sjtu.edu.cn":
            headers["jwt-token"] = self.video_token
        response = self._request(
            "GET",
            image_url,
            headers=headers,
            follow_redirects=True,
            service="PPT image",
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if not response.content:
            raise VideoIntegrationError("PPT image response was empty.")
        if content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
            raise VideoIntegrationError(
                f"PPT image response was not an image: {content_type}"
            )
        return response.content, content_type


def video_index_status() -> Dict[str, Any]:
    return {
        "status": "implemented-with-local-export",
        "auth": (
            "Canvas Access Token -> Canvas sessionless_launch -> SJTU video LTI "
            "jwt_token -> launch context teachingClassId"
        ),
        "read_only_features": [
            "list course videos",
            "read video playback metadata",
            "read subtitles and export SRT",
            "read PPT replay slice metadata",
        ],
        "local_export_features": [
            "download PPT replay images",
            "assemble PPT replay images into PDF",
        ],
        "optional_assets": "Null AI data means unavailable; playback/PPT video channels are separate assets.",
        "subtitle_source": (
            "Subtitles are fetched from SJTU video API v1/course/ai/translate; "
            "the CLI only converts returned subtitle rows to SRT."
        ),
        "not_implemented": [
            "video binary download",
            "any assignment submission workflow",
        ],
        "endpoints": {
            "canvas_sessionless_launch": (
                f"/api/v1/courses/{{course_id}}/external_tools/sessionless_launch?id={VIDEO_LTI_TOOL_ID}"
            ),
            "video_list": VIDEO_LIST_URL,
            "video_info": VIDEO_INFO_URL,
            "subtitle": SUBTITLE_URL,
            "ppt_slice": PPT_SLICE_URL,
        },
    }
