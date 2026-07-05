"""Minimal MCP stdio server for SJTU Canvas Assistant.

The official MCP Python SDK currently requires Python >=3.10. This project
supports the user's macOS system Python 3.9, so this module implements the
small stdio/JSON-RPC surface we need directly.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import __version__
from .canvas_client import CanvasClient
from .config import load_settings
from .exceptions import AssistantError
from .sync import CourseSyncer
from .video_assets import PPTAssetExporter
from .video_client import (
    SJTUVideoClient,
    redact_video_payload,
    subtitle_items_to_srt,
    video_index_status,
)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {PROTOCOL_VERSION, "2025-03-26"}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)


def _redact_canvas_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if (
                key_text in {"url", "preview_url"}
                and isinstance(item, str)
                and item.startswith(("http://", "https://"))
            ):
                redacted[str(key)] = "<redacted-url>"
            else:
                redacted[str(key)] = _redact_canvas_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_canvas_payload(item) for item in value]
    return value


def _schema(
    properties: Mapping[str, Mapping[str, Any]],
    *,
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": required or [],
        "additionalProperties": False,
    }


def _course_id_schema(extra: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    properties: Dict[str, Mapping[str, Any]] = {
        "course_id": {"type": "integer", "description": "Canvas course ID"}
    }
    if extra:
        properties.update(extra)
    return _schema(properties, required=["course_id"])


def _course_video_schema() -> Dict[str, Any]:
    return _schema(
        {
            "course_id": {"type": "integer", "description": "Canvas course ID"},
            "video_id": {
                "type": "string",
                "description": "SJTU video ID from list_course_videos",
            },
        },
        required=["course_id", "video_id"],
    )


def _result(payload: Any) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _pretty_json(payload)}],
        "structuredContent": json.loads(_json_dumps(payload)),
        "isError": False,
    }


def _tool_error(message: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Mapping[str, Any]], Any]

    def as_mcp_tool(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class CanvasMCPToolbox:
    """Canvas operations exposed as MCP tool handlers."""

    def __init__(self) -> None:
        self.settings = load_settings()

    def _client(self) -> CanvasClient:
        return CanvasClient(
            self.settings.base_url,
            self.settings.token,
            timeout_seconds=self.settings.timeout_seconds,
        )

    def _syncer(self, client: CanvasClient) -> CourseSyncer:
        return CourseSyncer(client, self.settings.data_dir)

    def _video_client(self) -> SJTUVideoClient:
        return SJTUVideoClient(
            self.settings.base_url,
            self.settings.token,
            timeout_seconds=self.settings.timeout_seconds,
            video_token=self.settings.video_token,
            video_course_id=self.settings.video_course_id,
        )

    @staticmethod
    def _output_dir(arguments: Mapping[str, Any]) -> Optional[Path]:
        raw = arguments.get("output_dir")
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str):
            raise ValueError("output_dir must be a string when provided")
        return Path(raw).expanduser()

    @staticmethod
    def _optional_path(arguments: Mapping[str, Any], key: str) -> Optional[Path]:
        raw = arguments.get(key)
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be a string when provided")
        return Path(raw).expanduser()

    @staticmethod
    def _course_id(arguments: Mapping[str, Any]) -> int:
        course_id = arguments.get("course_id")
        if not isinstance(course_id, int):
            raise ValueError("course_id must be an integer")
        return course_id

    @staticmethod
    def _video_id(arguments: Mapping[str, Any]) -> str:
        video_id = arguments.get("video_id")
        if not isinstance(video_id, str) or not video_id.strip():
            raise ValueError("video_id must be a non-empty string")
        return video_id.strip()

    @staticmethod
    def _optional_assignment_id(arguments: Mapping[str, Any]) -> Optional[int]:
        assignment_id = arguments.get("assignment_id")
        if assignment_id is None:
            return None
        if not isinstance(assignment_id, int):
            raise ValueError("assignment_id must be an integer when provided")
        return assignment_id

    def get_current_user(self, _: Mapping[str, Any]) -> Dict[str, Any]:
        with self._client() as client:
            return client.get_current_user().model_dump(mode="json")

    def list_courses(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        active_only = bool(arguments.get("active_only", False))
        with self._client() as client:
            return [
                course.model_dump(mode="json")
                for course in client.list_courses(active_only=active_only)
            ]

    def get_course(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        with self._client() as client:
            return client.get_course(course_id).model_dump(mode="json")

    def list_course_files(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        with self._client() as client:
            return [file.model_dump(mode="json") for file in client.list_course_files(course_id)]

    def list_course_folders(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        with self._client() as client:
            return [
                folder.model_dump(mode="json")
                for folder in client.list_course_folders(course_id)
            ]

    def list_course_modules(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        include_items = bool(arguments.get("include_items", True))
        with self._client() as client:
            return [
                module.model_dump(mode="json")
                for module in client.list_modules(course_id, include_items=include_items)
            ]

    def list_course_assignments(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        with self._client() as client:
            return [
                assignment.model_dump(mode="json")
                for assignment in client.list_assignments(course_id)
            ]

    def list_my_submissions(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        submitted_only = bool(arguments.get("submitted_only", True))
        with self._client() as client:
            result = self._syncer(client).fetch_my_submissions(
                course_id, submitted_only=submitted_only
            )
        return _redact_canvas_payload(result)

    def get_my_submission(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        assignment_id = self._optional_assignment_id(arguments)
        if assignment_id is None:
            raise ValueError("assignment_id is required")
        with self._client() as client:
            result = self._syncer(client).fetch_my_submission(course_id, assignment_id)
        return _redact_canvas_payload(result)

    def get_course_bundle(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        with self._client() as client:
            return self._syncer(client).fetch_course_bundle(course_id).model_dump(mode="json")

    def plan_course_download(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        output_dir = self._output_dir(arguments)
        with self._client() as client:
            syncer = self._syncer(client)
            bundle = syncer.fetch_course_bundle(course_id)
            records = syncer.plan_download_records(bundle, output_dir=output_dir)
        return {
            "course": bundle.course.model_dump(mode="json"),
            "summary": {
                "files": len(records),
                "bytes_to_fetch": sum(
                    record.size
                    for record in records
                    if record.status in {"would_download", "would_download_renamed"}
                ),
                "bytes_present": sum(record.size for record in records if record.status == "present"),
                "statuses": {
                    status: sum(1 for record in records if record.status == status)
                    for status in sorted({record.status for record in records})
                },
            },
            "downloads": [record.model_dump(mode="json") for record in records],
        }

    def generate_course_index(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        output_dir = self._output_dir(arguments)
        with self._client() as client:
            index, json_path, markdown_path = self._syncer(client).build_index_without_download(
                course_id, output_dir=output_dir
            )
        return {
            "course": index.course.model_dump(mode="json"),
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "downloads": [record.model_dump(mode="json") for record in index.downloads],
        }

    def download_course_files(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        output_dir = self._output_dir(arguments)
        force = bool(arguments.get("force", False))
        with self._client() as client:
            index, json_path, markdown_path = self._syncer(client).download_course_files(
                course_id, output_dir=output_dir, skip_existing=not force
            )
        return {
            "course": index.course.model_dump(mode="json"),
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "downloads": [record.model_dump(mode="json") for record in index.downloads],
        }

    def download_my_submission_files(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        assignment_id = self._optional_assignment_id(arguments)
        output_dir = self._output_dir(arguments)
        include_history = bool(arguments.get("include_history", True))
        force = bool(arguments.get("force", False))
        with self._client() as client:
            result = self._syncer(client).download_my_submission_files(
                course_id,
                assignment_id=assignment_id,
                output_dir=output_dir,
                include_history=include_history,
                skip_existing=not force,
            )
        return _redact_canvas_payload(result)

    def video_status(self, _: Mapping[str, Any]) -> Dict[str, Any]:
        return video_index_status()

    def resolve_video_auth(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        with self._video_client() as client:
            return client.resolve_course_video_auth(course_id, force=True).redacted()

    def list_course_videos(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        with self._video_client() as client:
            videos = client.list_course_videos(course_id)
        return redact_video_payload(videos)

    def get_video_info(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        video_id = self._video_id(arguments)
        with self._video_client() as client:
            info = client.get_video_info(course_id, video_id)
        return redact_video_payload(info)

    def get_video_subtitle(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        video_id = self._video_id(arguments)
        with self._video_client() as client:
            body = client.get_video_subtitle(course_id, video_id)
        srt = subtitle_items_to_srt(body.preferred_items)
        return redact_video_payload(
            {
                "subtitle": body.model_dump(mode="json"),
                "srt": srt,
                "srt_chars": len(srt),
            }
        )

    def get_video_ppt_slices(self, arguments: Mapping[str, Any]) -> List[Dict[str, Any]]:
        course_id = self._course_id(arguments)
        video_id = self._video_id(arguments)
        with self._video_client() as client:
            slices = client.get_video_ppt_slices(course_id, video_id)
        return redact_video_payload(slices)

    def download_video_ppt(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        course_id = self._course_id(arguments)
        video_id = self._video_id(arguments)
        output_dir = self._output_dir(arguments)
        pdf_path = self._optional_path(arguments, "pdf_path")
        make_pdf = bool(arguments.get("make_pdf", True))
        force = bool(arguments.get("force", False))
        with self._video_client() as client:
            result = PPTAssetExporter(client, self.settings.data_dir).export_video_ppt(
                course_id,
                video_id,
                output_dir=output_dir,
                pdf_path=pdf_path,
                make_pdf=make_pdf,
                force=force,
            )
        return redact_video_payload(result)


def build_tools(toolbox: CanvasMCPToolbox) -> Dict[str, ToolDefinition]:
    output_dir_property = {
        "output_dir": {
            "type": "string",
            "description": "Optional local output directory for generated indexes/downloads",
        }
    }
    tools = [
        ToolDefinition(
            "get_current_user",
            "Get Current User",
            "Validate the Canvas token and return the current user profile.",
            _schema({}),
            toolbox.get_current_user,
        ),
        ToolDefinition(
            "list_courses",
            "List Courses",
            "List Canvas courses visible to the configured token.",
            _schema(
                {
                    "active_only": {
                        "type": "boolean",
                        "description": "Only list active enrollments",
                        "default": False,
                    }
                }
            ),
            toolbox.list_courses,
        ),
        ToolDefinition(
            "get_course",
            "Get Course",
            "Return one Canvas course by ID.",
            _course_id_schema(),
            toolbox.get_course,
        ),
        ToolDefinition(
            "list_course_files",
            "List Course Files",
            "List files in one Canvas course.",
            _course_id_schema(),
            toolbox.list_course_files,
        ),
        ToolDefinition(
            "list_course_folders",
            "List Course Folders",
            "List folders in one Canvas course.",
            _course_id_schema(),
            toolbox.list_course_folders,
        ),
        ToolDefinition(
            "list_course_modules",
            "List Course Modules",
            "List modules and optionally module items in one Canvas course.",
            _course_id_schema(
                {
                    "include_items": {
                        "type": "boolean",
                        "description": "Fetch module items for each module",
                        "default": True,
                    }
                }
            ),
            toolbox.list_course_modules,
        ),
        ToolDefinition(
            "list_course_assignments",
            "List Course Assignments",
            "List assignments in one Canvas course. This is read-only and never submits work.",
            _course_id_schema(),
            toolbox.list_course_assignments,
        ),
        ToolDefinition(
            "list_my_submissions",
            "List My Submissions",
            "List my submissions for one Canvas course. This is read-only and returns redacted URLs.",
            _course_id_schema(
                {
                    "submitted_only": {
                        "type": "boolean",
                        "description": "Only include assignments with submitted work or attachments",
                        "default": True,
                    }
                }
            ),
            toolbox.list_my_submissions,
        ),
        ToolDefinition(
            "get_my_submission",
            "Get My Submission",
            "Read my submission for one assignment. This is read-only and returns redacted URLs.",
            _schema(
                {
                    "course_id": {"type": "integer", "description": "Canvas course ID"},
                    "assignment_id": {
                        "type": "integer",
                        "description": "Canvas assignment ID",
                    },
                },
                required=["course_id", "assignment_id"],
            ),
            toolbox.get_my_submission,
        ),
        ToolDefinition(
            "get_course_bundle",
            "Get Course Bundle",
            "Fetch course, folders, files, modules, and assignments in one call.",
            _course_id_schema(),
            toolbox.get_course_bundle,
        ),
        ToolDefinition(
            "plan_course_download",
            "Plan Course Download",
            "Preview download size, statuses, and local paths without downloading files.",
            _course_id_schema(output_dir_property),
            toolbox.plan_course_download,
        ),
        ToolDefinition(
            "generate_course_index",
            "Generate Course Index",
            "Generate local JSON/Markdown course indexes without downloading missing files.",
            _course_id_schema(output_dir_property),
            toolbox.generate_course_index,
        ),
        ToolDefinition(
            "download_course_files",
            "Download Course Files",
            "Download course files locally and generate indexes. Existing same-size files are skipped unless force is true.",
            _course_id_schema(
                {
                    **output_dir_property,
                    "force": {
                        "type": "boolean",
                        "description": "Re-download even when a same-size local file exists",
                        "default": False,
                    },
                }
            ),
            toolbox.download_course_files,
        ),
        ToolDefinition(
            "download_my_submission_files",
            "Download My Submission Files",
            "Download my submitted assignment attachments locally. This never submits or modifies work.",
            _course_id_schema(
                {
                    **output_dir_property,
                    "assignment_id": {
                        "type": "integer",
                        "description": "Optional assignment ID; omit to download all assignment submissions",
                    },
                    "include_history": {
                        "type": "boolean",
                        "description": "Include attachments from submission history attempts",
                        "default": True,
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Re-download even when a same-size local file exists",
                        "default": False,
                    },
                }
            ),
            toolbox.download_my_submission_files,
        ),
        ToolDefinition(
            "video_status",
            "Video Integration Status",
            "Return the implemented SJTU video/subtitle/PPT integration flow.",
            _schema({}),
            toolbox.video_status,
        ),
        ToolDefinition(
            "resolve_video_auth",
            "Resolve Video Auth",
            "Resolve SJTU video auth for one Canvas course and return only redacted auth metadata.",
            _course_id_schema(),
            toolbox.resolve_video_auth,
        ),
        ToolDefinition(
            "list_course_videos",
            "List Course Videos",
            "List SJTU classroom videos for one Canvas course.",
            _course_id_schema(),
            toolbox.list_course_videos,
        ),
        ToolDefinition(
            "get_video_info",
            "Get Video Info",
            "Read playback metadata for one SJTU video. Returned payload is token-redacted.",
            _course_video_schema(),
            toolbox.get_video_info,
        ),
        ToolDefinition(
            "get_video_subtitle",
            "Get Video Subtitle",
            "Read subtitle rows for one SJTU video and return generated SRT text.",
            _course_video_schema(),
            toolbox.get_video_subtitle,
        ),
        ToolDefinition(
            "get_video_ppt_slices",
            "Get Video PPT Slices",
            "Read PPT replay slice metadata for one SJTU video. This does not download images.",
            _course_video_schema(),
            toolbox.get_video_ppt_slices,
        ),
        ToolDefinition(
            "download_video_ppt",
            "Download Video PPT",
            "Download PPT replay images for one SJTU video and optionally assemble a local PDF.",
            _schema(
                {
                    "course_id": {"type": "integer", "description": "Canvas course ID"},
                    "video_id": {
                        "type": "string",
                        "description": "SJTU video ID from list_course_videos",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Optional output directory for PPT images and default PDF",
                    },
                    "pdf_path": {
                        "type": "string",
                        "description": "Optional PDF output path",
                    },
                    "make_pdf": {
                        "type": "boolean",
                        "description": "Assemble downloaded images into a PDF",
                        "default": True,
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Re-download existing images and regenerate PDF",
                        "default": False,
                    },
                },
                required=["course_id", "video_id"],
            ),
            toolbox.download_video_ppt,
        ),
    ]
    return {tool.name: tool for tool in tools}


class MCPStdioServer:
    def __init__(self, tools: Mapping[str, ToolDefinition]) -> None:
        self.tools = dict(tools)
        self.protocol_version = PROTOCOL_VERSION

    def handle_message(self, message: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._error(request_id, JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC request")
        if request_id is None and method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self._response(request_id, self._initialize(message.get("params") or {}))
        if method == "tools/list":
            return self._response(request_id, {"tools": [tool.as_mcp_tool() for tool in self.tools.values()]})
        if method == "tools/call":
            return self._response(request_id, self._call_tool(message.get("params") or {}))
        if method == "ping":
            return self._response(request_id, {})
        if method.startswith("notifications/"):
            return None
        return self._error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method}")

    def _initialize(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        self.protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "sjtu-canvas-assistant", "version": __version__},
            "instructions": (
                "Use these tools to read SJTU Canvas courses, generate local indexes, "
                "download course files, and read/export SJTU video metadata/subtitles/PPT assets. "
                "Assignment tools are read-only unless explicitly downloading your own submitted files; "
                "no tool submits work. Video export tools only write local files."
            ),
        }

    def _call_tool(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        if not isinstance(arguments, Mapping):
            raise ValueError("Tool arguments must be an object")
        try:
            payload = self.tools[name].handler(arguments)
        except AssistantError as exc:
            return _tool_error(str(exc))
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr)
            return _tool_error(f"{type(exc).__name__}: {exc}")
        return _result(payload)

    @staticmethod
    def _response(request_id: Any, result: Mapping[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def serve_stdio(server: MCPStdioServer) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = MCPStdioServer._error(None, JSONRPC_PARSE_ERROR, str(exc))
        else:
            try:
                response = server.handle_message(message)
            except ValueError as exc:
                response = MCPStdioServer._error(
                    message.get("id") if isinstance(message, Mapping) else None,
                    JSONRPC_INVALID_PARAMS,
                    str(exc),
                )
            except Exception as exc:
                print(traceback.format_exc(), file=sys.stderr)
                response = MCPStdioServer._error(
                    message.get("id") if isinstance(message, Mapping) else None,
                    JSONRPC_INTERNAL_ERROR,
                    str(exc),
                )
        if response is not None:
            sys.stdout.write(_json_dumps(response) + "\n")
            sys.stdout.flush()


def main() -> None:
    toolbox = CanvasMCPToolbox()
    serve_stdio(MCPStdioServer(build_tools(toolbox)))


if __name__ == "__main__":
    main()
