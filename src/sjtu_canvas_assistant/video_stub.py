"""Reserved SJTU video/subtitle integration surface.

The first release intentionally avoids browser automation and jAccount password
handling. SJTU course video access is not exposed by the Canvas token alone.
The reference project reaches it through the Canvas external tool page and the
SJTU video site's own token/cookie flow.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .exceptions import VideoIntegrationNotImplemented

VIDEO_BASE_URL = "https://courses.sjtu.edu.cn/app"
VIDEO_LTI_TOOL_ID = 8329
VIDEO_LTI_FORM_ACTION = (
    "https://v.sjtu.edu.cn/jy-application-canvas-sjtu/oidc/login_initiations"
)
VIDEO_TOKEN_BY_ID_URL = (
    "https://v.sjtu.edu.cn/jy-application-canvas-sjtu/lti3/getAccessTokenByTokenId"
)
VIDEO_LIST_URL = (
    "https://v.sjtu.edu.cn/jy-application-canvas-sjtu/directOnDemandPlay/findVodVideoList"
)
VIDEO_INFO_URL = (
    "https://v.sjtu.edu.cn/jy-application-canvas-sjtu/directOnDemandPlay/getVodVideoInfos"
)
SUBTITLE_URL = "https://v.sjtu.edu.cn/jy-application-canvas-sjtu/transfer/translate/detail"
PPT_SLICE_URL = (
    "https://v.sjtu.edu.cn/jy-application-canvas-sjtu/"
    "directOnDemandPlay/vod-analysis/query-ppt-slice-es"
)


class SJTUVideoClient:
    """Placeholder interface for the future SJTU video integration."""

    def list_course_videos(self, course_id: int) -> List[Dict[str, Any]]:
        raise VideoIntegrationNotImplemented(_message(course_id))

    def get_video_info(self, video_id: str) -> Dict[str, Any]:
        raise VideoIntegrationNotImplemented(
            "Video info is not implemented in v1. It requires a video-site token."
        )

    def get_subtitle(self, canvas_video_course_id: int) -> str:
        raise VideoIntegrationNotImplemented(
            "Subtitle download is not implemented in v1. It requires the SJTU video API token."
        )

    def download_ppt_pdf(self, canvas_video_course_id: int, output_path: str) -> None:
        raise VideoIntegrationNotImplemented(
            "PPT replay export is not implemented in v1. It requires the SJTU video API token."
        )


def _message(course_id: int) -> str:
    return (
        f"Video listing for course {course_id} is not implemented in v1. "
        "The planned flow is: authenticate Canvas web session, open "
        f"/courses/{course_id}/external_tools/{VIDEO_LTI_TOOL_ID}, parse the LTI/OIDC forms, "
        "exchange tokenId for the SJTU video API token and canvasCourseId, then call "
        "findVodVideoList/getVodVideoInfos plus subtitle/PPT endpoints."
    )


def video_index_stub() -> Dict[str, Any]:
    return {
        "status": "planned",
        "note": (
            "Video/subtitle/PPT replay support is reserved for a later release. "
            "SJTU video access needs the Canvas external tool LTI flow plus SJTU video-site "
            "cookies/token; Canvas Access Token alone is not enough."
        ),
        "reference_flow": [
            f"GET /courses/{{course_id}}/external_tools/{VIDEO_LTI_TOOL_ID} after Canvas web login",
            f"POST LTI form to {VIDEO_LTI_FORM_ACTION}",
            f"Exchange tokenId through {VIDEO_TOKEN_BY_ID_URL}",
            f"List videos through {VIDEO_LIST_URL}",
            f"Fetch playable streams through {VIDEO_INFO_URL}",
            f"Fetch subtitles through {SUBTITLE_URL}",
            f"Fetch PPT slices through {PPT_SLICE_URL}",
        ],
    }
