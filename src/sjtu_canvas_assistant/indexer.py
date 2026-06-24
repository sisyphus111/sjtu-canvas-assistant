"""JSON and Markdown index generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Tuple

from .models import Assignment, CanvasFile, CourseIndex, DownloadRecord, Module
from .path_utils import course_dir_name


def _md_escape(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: List[str], rows: Iterable[Iterable[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_escape(cell) for cell in row) + " |")
    return "\n".join(lines)


def _assignment_due(assignment: Assignment) -> str:
    if assignment.due_at:
        return assignment.due_at
    for date in assignment.all_dates:
        if date.base and date.due_at:
            return date.due_at
    return ""


def _render_modules(modules: List[Module]) -> str:
    if not modules:
        return "No modules found.\n"
    parts: List[str] = []
    for module in sorted(modules, key=lambda item: item.position):
        parts.append(f"### {_md_escape(module.name or module.id)}")
        if not module.items:
            parts.append("")
            parts.append("_No module items._")
            continue
        rows = []
        for item in sorted(module.items, key=lambda entry: entry.position):
            rows.append(
                [
                    item.position,
                    item.type,
                    item.title,
                    item.content_details.due_at if item.content_details else "",
                    item.html_url or item.external_url or "",
                ]
            )
        parts.append(_table(["#", "Type", "Title", "Due", "URL"], rows))
    return "\n\n".join(parts) + "\n"


def render_markdown_index(index: CourseIndex) -> str:
    course = index.course
    teachers = ", ".join(t.display_name for t in course.teachers if t.display_name)
    term = course.term.name if course.term else ""

    summary = _table(
        ["Field", "Value"],
        [
            ["Course", course.display_name],
            ["Course ID", course.id],
            ["Course Code", course.course_code],
            ["Term", term],
            ["Teachers", teachers],
            ["Canvas Base URL", index.base_url],
            ["Generated At", index.generated_at],
            ["Files", len(index.files)],
            ["Folders", len(index.folders)],
            ["Modules", len(index.modules)],
            ["Assignments", len(index.assignments)],
        ],
    )

    file_by_id = {file.id: file for file in index.files}
    downloads = _table(
        ["Status", "Canvas Path", "Local Path", "Size", "Updated", "Message"],
        [
            [
                record.status,
                record.canvas_path,
                record.relative_download_path or record.download_path or "",
                record.size,
                record.updated_at or "",
                record.message,
            ]
            for record in index.downloads
        ],
    )

    files = _table(
        ["ID", "Name", "Folder ID", "Size", "Updated", "Download URL"],
        [
            [
                file.id,
                file.effective_name,
                file.folder_id or "",
                file.size,
                file.updated_at or file.modified_at or "",
                "yes" if file.url else "no",
            ]
            for file in sorted(index.files, key=lambda item: item.effective_name.lower())
        ],
    )

    assignments = _table(
        ["ID", "Name", "Due", "Points", "Submission Types", "URL"],
        [
            [
                assignment.id,
                assignment.name,
                _assignment_due(assignment),
                assignment.points_possible if assignment.points_possible is not None else "",
                ", ".join(assignment.submission_types),
                assignment.html_url,
            ]
            for assignment in sorted(index.assignments, key=lambda item: item.name.lower())
        ],
    )

    video_note = index.video.get("note", "")
    mcp_note = index.mcp.get("note", "")

    return "\n".join(
        [
            f"# {course.display_name}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Downloads",
            "",
            downloads if index.downloads else "No download records.",
            "",
            "## Files",
            "",
            files if file_by_id else "No files found.",
            "",
            "## Assignments",
            "",
            assignments if index.assignments else "No assignments found.",
            "",
            "## Modules",
            "",
            _render_modules(index.modules),
            "## Video And Subtitles",
            "",
            video_note,
            "",
            "## Future MCP",
            "",
            mcp_note,
            "",
        ]
    )


def write_index(index: CourseIndex, output_root: Path) -> Tuple[CourseIndex, Path, Path]:
    course_dir = output_root / course_dir_name(index.course)
    course_dir.mkdir(parents=True, exist_ok=True)
    json_path = course_dir / "course_index.json"
    md_path = course_dir / "course_index.md"

    json_payload = index.model_dump(mode="json")
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown_index(index), encoding="utf-8")
    return index, json_path, md_path
