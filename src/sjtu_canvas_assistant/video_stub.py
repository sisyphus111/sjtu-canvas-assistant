"""Backward-compatible imports for the v1 video integration module."""

from __future__ import annotations

from .video_client import (
    PPT_SLICE_URL,
    SUBTITLE_URL,
    VIDEO_INFO_URL,
    VIDEO_LIST_URL,
    VIDEO_LTI_FORM_ACTION,
    VIDEO_LTI_TOOL_ID,
    VIDEO_TOKEN_BY_ID_URL,
    SJTUVideoClient,
    video_index_status,
)


def video_index_stub():
    """Compatibility alias retained for older code/tests."""

    return video_index_status()
