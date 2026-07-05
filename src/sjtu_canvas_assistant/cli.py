"""Command-line interface for SJTU Canvas Assistant."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .canvas_client import CanvasClient
from .config import load_settings
from .exceptions import AssistantError
from .models import (
    Assignment,
    CanvasFile,
    CanvasVideo,
    Course,
    CourseBundle,
    DownloadRecord,
    Folder,
    Module,
    MySubmission,
    PPTExportResult,
    SubmissionDownloadResult,
    VideoInfo,
    VideoPPTSlice,
    VideoSubtitleBody,
)
from .mcp_server import main as run_mcp_server
from .path_utils import safe_path_segment
from .sync import CourseSyncer
from .video_assets import PPTAssetExporter
from .video_client import (
    SJTUVideoClient,
    redact_video_payload,
    subtitle_items_to_srt,
    video_index_status,
)

app = typer.Typer(
    name="sjtu-canvas",
    no_args_is_help=True,
    help="Local CLI framework for SJTU Canvas courses, files, assignments, and indexes.",
)
console = Console()
FETCH_STATUSES = {"would_download", "would_download_renamed"}
video_app = typer.Typer(
    no_args_is_help=True,
    help="Read SJTU course videos, subtitles, and PPT replay metadata.",
)
app.add_typer(video_app, name="video")


def _exit_with_error(exc: Exception) -> None:
    console.print(f"[red]Error:[/red] {exc}")
    raise typer.Exit(1)


def _make_client(
    token: Optional[str],
    base_url: Optional[str],
    data_dir: Optional[Path],
) -> tuple[CanvasClient, Path]:
    settings = load_settings(token=token, base_url=base_url, data_dir=data_dir)
    return (
        CanvasClient(
            settings.base_url,
            settings.token,
            timeout_seconds=settings.timeout_seconds,
        ),
        settings.data_dir,
    )


def _make_video_client(
    token: Optional[str],
    base_url: Optional[str],
    data_dir: Optional[Path],
) -> tuple[SJTUVideoClient, Path]:
    settings = load_settings(token=token, base_url=base_url, data_dir=data_dir)
    return (
        SJTUVideoClient(
            settings.base_url,
            settings.token,
            timeout_seconds=settings.timeout_seconds,
            video_token=settings.video_token,
            video_course_id=settings.video_course_id,
        ),
        settings.data_dir,
    )


def _print_json(value: object) -> None:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
    else:
        payload = value
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _redact_canvas_payload(value: object) -> object:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[attr-defined]
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if (
                key_text in {"url", "preview_url"}
                and isinstance(item, str)
                and item.startswith(("http://", "https://"))
            ):
                redacted[key] = "<redacted-url>"
            else:
                redacted[key] = _redact_canvas_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_canvas_payload(item) for item in value]
    return value


def _print_canvas_json(value: object) -> None:
    typer.echo(json.dumps(_redact_canvas_payload(value), ensure_ascii=False, indent=2))


def _print_video_json(value: object) -> None:
    typer.echo(json.dumps(redact_video_payload(value), ensure_ascii=False, indent=2))


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ["B", "KiB", "MiB", "GiB"]:
        if value < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _teacher_names(course: Course) -> str:
    return ", ".join(t.display_name for t in course.teachers if t.display_name)


def _enrollment_roles(course: Course) -> str:
    roles = []
    for enrollment in course.enrollments:
        role = enrollment.role or enrollment.type
        if role and role not in roles:
            roles.append(role)
    return ", ".join(roles)


def _render_courses(courses: list[Course]) -> None:
    table = Table(title="Canvas Courses")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Code")
    table.add_column("Term")
    table.add_column("Role")
    table.add_column("Teachers")
    for course in courses:
        table.add_row(
            str(course.id),
            course.display_name,
            course.course_code,
            course.term.name if course.term else "",
            _enrollment_roles(course),
            _teacher_names(course),
        )
    console.print(table)


def _render_files(files: list[CanvasFile]) -> None:
    table = Table(title="Course Files")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Folder", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Updated")
    table.add_column("Locked")
    for file in files:
        table.add_row(
            str(file.id),
            file.effective_name,
            str(file.folder_id or ""),
            str(file.size),
            file.updated_at or file.modified_at or "",
            "yes" if file.locked else "",
        )
    console.print(table)


def _render_folders(folders: list[Folder]) -> None:
    table = Table(title="Course Folders")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Full Name")
    table.add_column("Files", justify="right")
    table.add_column("Folders", justify="right")
    for folder in folders:
        table.add_row(
            str(folder.id),
            folder.name,
            folder.full_name,
            str(folder.files_count),
            str(folder.folders_count),
        )
    console.print(table)


def _render_modules(modules: list[Module]) -> None:
    table = Table(title="Course Modules")
    table.add_column("#", justify="right")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Items", justify="right")
    table.add_column("State")
    for module in modules:
        table.add_row(
            str(module.position),
            str(module.id),
            module.name,
            str(module.items_count or len(module.items)),
            module.state or "",
        )
        for item in sorted(module.items, key=lambda entry: entry.position):
            table.add_row(
                "",
                f"  {item.id}",
                f"  [{item.type}] {item.title}",
                "",
                item.content_details.due_at if item.content_details else "",
            )
    console.print(table)


def _assignment_due(assignment: Assignment) -> str:
    if assignment.due_at:
        return assignment.due_at
    for date in assignment.all_dates:
        if date.base and date.due_at:
            return date.due_at
    return ""


def _render_assignments(assignments: list[Assignment]) -> None:
    table = Table(title="Course Assignments")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Due")
    table.add_column("Points", justify="right")
    table.add_column("Types")
    table.add_column("Published")
    for assignment in assignments:
        table.add_row(
            str(assignment.id),
            assignment.name,
            _assignment_due(assignment),
            "" if assignment.points_possible is None else str(assignment.points_possible),
            ", ".join(assignment.submission_types),
            "" if assignment.published is None else ("yes" if assignment.published else "no"),
        )
    console.print(table)


def _submission_attachment_count(item: MySubmission) -> int:
    submission = item.submission
    attachment_ids = {attachment.id for attachment in submission.attachments}
    for attempt in submission.submission_history:
        attachment_ids.update(attachment.id for attachment in attempt.attachments)
    return len(attachment_ids)


def _render_submissions(submissions: list[MySubmission]) -> None:
    table = Table(title="My Canvas Submissions")
    table.add_column("Assignment ID", justify="right")
    table.add_column("Assignment")
    table.add_column("Type")
    table.add_column("State")
    table.add_column("Submitted At")
    table.add_column("Attempt", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Score", justify="right")
    for item in submissions:
        submission = item.submission
        score = ""
        if submission.score is not None:
            score = str(submission.score)
        elif submission.grade:
            score = submission.grade
        table.add_row(
            str(item.assignment.id),
            item.assignment.name,
            submission.submission_type,
            submission.workflow_state,
            submission.submitted_at or "",
            "" if submission.attempt is None else str(submission.attempt),
            str(_submission_attachment_count(item)),
            score,
        )
    console.print(table)


def _render_submission_detail(item: MySubmission) -> None:
    submission = item.submission
    console.print(f"[bold]{item.assignment.name}[/bold] ({item.assignment.id})")
    table = Table(title="My Submission")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in [
        ("submission_id", submission.id or ""),
        ("workflow_state", submission.workflow_state),
        ("submission_type", submission.submission_type),
        ("submitted_at", submission.submitted_at or ""),
        ("attempt", submission.attempt or ""),
        ("score", submission.score if submission.score is not None else ""),
        ("grade", submission.grade or ""),
        ("late", "" if submission.late is None else submission.late),
        ("missing", "" if submission.missing is None else submission.missing),
        ("attachments", _submission_attachment_count(item)),
    ]:
        table.add_row(key, str(value))
    console.print(table)

    attachment_table = Table(title="Submission Attachments")
    attachment_table.add_column("Attempt", justify="right")
    attachment_table.add_column("File ID", justify="right")
    attachment_table.add_column("Name")
    attachment_table.add_column("Size", justify="right")
    attachment_table.add_column("Updated")
    seen = set()
    for attempt in submission.submission_history:
        for attachment in attempt.attachments:
            seen.add((attempt.attempt, attachment.id))
            attachment_table.add_row(
                "" if attempt.attempt is None else str(attempt.attempt),
                str(attachment.id),
                attachment.effective_name,
                _format_bytes(attachment.size),
                attachment.updated_at or attachment.modified_at or "",
            )
    for attachment in submission.attachments:
        key = (submission.attempt, attachment.id)
        if key in seen:
            continue
        attachment_table.add_row(
            "" if submission.attempt is None else str(submission.attempt),
            str(attachment.id),
            attachment.effective_name,
            _format_bytes(attachment.size),
            attachment.updated_at or attachment.modified_at or "",
        )
    console.print(attachment_table)


def _render_videos(videos: list[CanvasVideo]) -> None:
    table = Table(title="Course Videos")
    table.add_column("Video ID")
    table.add_column("Name")
    table.add_column("Teacher")
    table.add_column("Classroom")
    table.add_column("Begin")
    table.add_column("End")
    for video in videos:
        table.add_row(
            video.video_id,
            video.display_name,
            video.user_name,
            video.classroom_name,
            video.course_begin_time,
            video.course_end_time,
        )
    console.print(table)


def _render_video_info(info: VideoInfo) -> None:
    table = Table(title="Video Info")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in [
        ("id", info.id),
        ("video_id", info.video_id),
        ("name", info.video_name),
        ("teacher", info.user_name),
        ("classroom", info.classroom_name),
        ("begin", info.course_begin_time),
        ("end", info.course_end_time),
        ("video_course_id", info.cour_id),
        ("play_entries", len(info.play_items)),
    ]:
        table.add_row(key, str(value or ""))
    console.print(table)


def _render_subtitle_summary(body: VideoSubtitleBody, srt: str, output: Optional[Path]) -> None:
    console.print(
        f"Subtitle rows: before={len(body.before_assembly_list)}  "
        f"after={len(body.after_assembly_list)}  srt_chars={len(srt)}"
    )
    if output is not None:
        console.print(f"SRT output: {output}")


def _short_text(value: object, *, limit: int = 120) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(redact_video_payload(value), ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ocr_summary(value: object, *, limit: int = 120) -> str:
    if isinstance(value, list):
        words = []
        for item in value:
            if isinstance(item, dict) and item.get("word"):
                words.append(str(item["word"]))
            elif isinstance(item, str):
                words.append(item)
        if words:
            return _short_text(" ".join(words), limit=limit)
    if isinstance(value, dict) and value.get("word"):
        return _short_text(value["word"], limit=limit)
    return _short_text(value, limit=limit)


def _format_video_offset(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds)) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _render_ppt_slices(slices: list[VideoPPTSlice]) -> None:
    table = Table(title="Video PPT Slices")
    table.add_column("Offset", justify="right")
    table.add_column("OCR")
    table.add_column("Image")
    for item in slices:
        table.add_row(
            _format_video_offset(item.create_sec),
            _ocr_summary(item.ocr),
            "yes" if item.ppt_img_url else "",
        )
    console.print(table)


def _render_ppt_export_result(result: PPTExportResult) -> None:
    console.print(
        "  ".join(
            [
                f"slices: {result.slices_count}",
                "statuses: "
                + ", ".join(
                    f"{status}={count}" for status, count in sorted(result.summary.items())
                ),
            ]
        )
    )
    console.print(f"Image directory: {result.image_dir}")
    if result.pdf_path is not None:
        console.print(f"PDF: {result.pdf_path} ({result.pdf_status or 'not generated'})")

    table = Table(title="PPT Image Export")
    table.add_column("#", justify="right")
    table.add_column("Offset", justify="right")
    table.add_column("Status")
    table.add_column("Size", justify="right")
    table.add_column("Path")
    table.add_column("Message")
    for record in result.images:
        table.add_row(
            str(record.slice_index),
            _format_video_offset(record.offset_ms),
            record.status,
            _format_bytes(record.size),
            record.relative_image_path or str(record.image_path or ""),
            record.message,
        )
    console.print(table)


def _render_submission_download_result(result: SubmissionDownloadResult) -> None:
    console.print(
        "  ".join(
            [
                f"assignments: {len(result.submissions)}",
                f"files: {len(result.downloads)}",
                "statuses: "
                + ", ".join(
                    f"{status}={count}" for status, count in sorted(result.summary.items())
                ),
            ]
        )
    )
    table = Table(title="Submission File Downloads")
    table.add_column("Status")
    table.add_column("Assignment")
    table.add_column("Attempt", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Local Path")
    table.add_column("Message")
    for record in result.downloads:
        table.add_row(
            record.status,
            record.assignment_name,
            "" if record.attempt is None else str(record.attempt),
            _format_bytes(record.size),
            record.relative_download_path or str(record.download_path or ""),
            record.message,
        )
    console.print(table)


def _render_download_plan(records: list[DownloadRecord], *, limit: int) -> None:
    total_size = sum(record.size for record in records if record.status in FETCH_STATUSES)
    existing_size = sum(record.size for record in records if record.status == "present")
    counts = Counter(record.status for record in records)
    console.print(
        "  ".join(
            [
                f"files: {len(records)}",
                f"to fetch: {_format_bytes(total_size)}",
                f"already present: {_format_bytes(existing_size)}",
                "statuses: "
                + ", ".join(f"{status}={count}" for status, count in sorted(counts.items())),
            ]
        )
    )

    table = Table(title="Download Plan")
    table.add_column("Status")
    table.add_column("Size", justify="right")
    table.add_column("Canvas Path")
    table.add_column("Local Path")
    table.add_column("Message")
    for record in records[:limit]:
        table.add_row(
            record.status,
            _format_bytes(record.size),
            record.canvas_path,
            record.relative_download_path or str(record.download_path or ""),
            record.message,
        )
    console.print(table)
    if len(records) > limit:
        console.print(f"... {len(records) - limit} more files hidden. Use --limit {len(records)} to show all.")


@app.command("whoami")
def whoami(
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Validate the token and show the current Canvas user."""

    try:
        client, _ = _make_client(token, base_url, data_dir)
        with client:
            user = client.get_current_user()
        if json_output:
            _print_json(user)
            return
        table = Table(title="Current Canvas User")
        table.add_column("Field")
        table.add_column("Value")
        for key, value in [
            ("id", user.id),
            ("name", user.name),
            ("short_name", user.short_name),
            ("login_id", user.login_id),
            ("email", user.email or ""),
        ]:
            table.add_row(key, str(value))
        console.print(table)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("courses")
def courses(
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    active_only: bool = typer.Option(False, "--active-only", help="Only list active enrollments."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List courses visible to the current token."""

    try:
        client, _ = _make_client(token, base_url, data_dir)
        with client:
            result = client.list_courses(active_only=active_only)
        if json_output:
            _print_json([course.model_dump(mode="json") for course in result])
        else:
            _render_courses(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("course")
def course(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List files, folders, modules, and assignments for one course."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            bundle = CourseSyncer(client, root).fetch_course_bundle(course_id)
        if json_output:
            _print_json(bundle)
            return
        _render_course_bundle(bundle)
    except AssistantError as exc:
        _exit_with_error(exc)


def _render_course_bundle(bundle: CourseBundle) -> None:
    console.print(f"[bold]{bundle.course.display_name}[/bold] ({bundle.course.id})")
    console.print(
        f"Files: {len(bundle.files)}  Folders: {len(bundle.folders)}  "
        f"Modules: {len(bundle.modules)}  Assignments: {len(bundle.assignments)}"
    )
    _render_folders(bundle.folders)
    _render_files(bundle.files)
    _render_modules(bundle.modules)
    _render_assignments(bundle.assignments)


@app.command("files")
def files(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List files for one course."""

    try:
        client, _ = _make_client(token, base_url, data_dir)
        with client:
            result = client.list_course_files(course_id)
        if json_output:
            _print_json([file.model_dump(mode="json") for file in result])
        else:
            _render_files(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("folders")
def folders(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List folders for one course."""

    try:
        client, _ = _make_client(token, base_url, data_dir)
        with client:
            result = client.list_course_folders(course_id)
        if json_output:
            _print_json([folder.model_dump(mode="json") for folder in result])
        else:
            _render_folders(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("modules")
def modules(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List modules and module items for one course."""

    try:
        client, _ = _make_client(token, base_url, data_dir)
        with client:
            result = client.list_modules(course_id, include_items=True)
        if json_output:
            _print_json([module.model_dump(mode="json") for module in result])
        else:
            _render_modules(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("assignments")
def assignments(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List assignments for one course. This command never submits assignments."""

    try:
        client, _ = _make_client(token, base_url, data_dir)
        with client:
            result = client.list_assignments(course_id)
        if json_output:
            _print_json([assignment.model_dump(mode="json") for assignment in result])
        else:
            _render_assignments(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("submissions")
def submissions(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(
        None, "--data-dir", help="Local data directory."
    ),
    show_all: bool = typer.Option(
        False, "--all", help="Include assignments with no submitted work."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """List my submissions for one course. This command is read-only."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            result = CourseSyncer(client, root).fetch_my_submissions(
                course_id, submitted_only=not show_all
            )
        if json_output:
            _print_canvas_json([item.model_dump(mode="json") for item in result])
        else:
            _render_submissions(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("submission")
def submission(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    assignment_id: int = typer.Argument(..., help="Canvas assignment ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(
        None, "--data-dir", help="Local data directory."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """Show my submission for one assignment. This command is read-only."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            result = CourseSyncer(client, root).fetch_my_submission(course_id, assignment_id)
        if json_output:
            _print_canvas_json(result)
        else:
            _render_submission_detail(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("download-submissions")
def download_submissions(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(
        None, "--data-dir", help="Local data directory."
    ),
    assignment_id: Optional[int] = typer.Option(
        None, "--assignment-id", help="Only download one assignment."
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Download output directory."
    ),
    latest_only: bool = typer.Option(
        False, "--latest-only", help="Download only the latest submission attachment set."
    ),
    force: bool = typer.Option(False, "--force", help="Re-download existing same-size files."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """Download my submitted assignment attachments. This never submits work."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            result = CourseSyncer(client, root).download_my_submission_files(
                course_id,
                assignment_id=assignment_id,
                output_dir=output_dir,
                include_history=not latest_only,
                skip_existing=not force,
            )
        if json_output:
            _print_canvas_json(result)
        else:
            _render_submission_download_result(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("download")
def download(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Download/index output directory."),
    force: bool = typer.Option(False, "--force", help="Download even when an existing same-size file is present."),
) -> None:
    """Download course files and generate local JSON/Markdown indexes."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            index, json_path, md_path = CourseSyncer(client, root).download_course_files(
                course_id,
                output_dir=output_dir,
                skip_existing=not force,
            )
        counts = Counter(record.status for record in index.downloads)
        console.print(f"[bold]Download complete for {index.course.display_name}[/bold]")
        console.print(
            "  "
            + "  ".join(
                f"{status}: {count}" for status, count in sorted(counts.items())
            )
        )
        console.print(f"JSON index: {json_path}")
        console.print(f"Markdown index: {md_path}")
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("plan")
def plan(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Planned download/index output directory."),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum planned files to show in the table."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Preview course download size, statuses, and local paths without downloading."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            syncer = CourseSyncer(client, root)
            bundle = syncer.fetch_course_bundle(course_id)
            records = syncer.plan_download_records(bundle, output_dir=output_dir)
        if json_output:
            _print_json(
                {
                    "course": bundle.course.model_dump(mode="json"),
                    "summary": {
                        "files": len(records),
                        "bytes_to_fetch": sum(
                            record.size
                            for record in records
                            if record.status in FETCH_STATUSES
                        ),
                        "bytes_present": sum(record.size for record in records if record.status == "present"),
                        "statuses": dict(Counter(record.status for record in records)),
                    },
                    "downloads": [record.model_dump(mode="json") for record in records],
                }
            )
            return
        console.print(f"[bold]Download plan for {bundle.course.display_name}[/bold]")
        _render_download_plan(records, limit=limit)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("index")
def index(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Index output directory."),
) -> None:
    """Generate JSON/Markdown indexes without downloading missing files."""

    try:
        client, root = _make_client(token, base_url, data_dir)
        with client:
            course_index, json_path, md_path = CourseSyncer(client, root).build_index_without_download(
                course_id,
                output_dir=output_dir,
            )
        console.print(f"[bold]Index generated for {course_index.course.display_name}[/bold]")
        console.print(f"JSON index: {json_path}")
        console.print(f"Markdown index: {md_path}")
    except AssistantError as exc:
        _exit_with_error(exc)


@video_app.command("status")
def video_status_command() -> None:
    """Show the implemented SJTU video/subtitle integration surface."""

    console.print_json(json.dumps(video_index_status(), ensure_ascii=False))


@video_app.command("auth")
def video_auth(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """Resolve SJTU video auth for one course without printing secret values."""

    try:
        client, _ = _make_video_client(token, base_url, data_dir)
        with client:
            auth = client.resolve_course_video_auth(course_id, force=True)
        payload = auth.redacted()
        if json_output:
            _print_video_json(payload)
            return
        table = Table(title="SJTU Video Auth")
        table.add_column("Field")
        table.add_column("Value")
        for key, value in payload.items():
            table.add_row(key, str(value))
        console.print(table)
    except AssistantError as exc:
        _exit_with_error(exc)


@video_app.command("list")
def video_list(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """List videos exposed by SJTU's Canvas video integration."""

    try:
        client, _ = _make_video_client(token, base_url, data_dir)
        with client:
            videos = client.list_course_videos(course_id)
        if json_output:
            _print_video_json([video.model_dump(mode="json") for video in videos])
        else:
            _render_videos(videos)
    except AssistantError as exc:
        _exit_with_error(exc)


@video_app.command("info")
def video_info(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    video_id: str = typer.Argument(..., help="SJTU video ID from `video list`."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """Read playback metadata for one video. This does not download video bytes."""

    try:
        client, _ = _make_video_client(token, base_url, data_dir)
        with client:
            info = client.get_video_info(course_id, video_id)
        if json_output:
            _print_video_json(info)
        else:
            _render_video_info(info)
    except AssistantError as exc:
        _exit_with_error(exc)


@video_app.command("subtitle")
def video_subtitle(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    video_id: str = typer.Argument(..., help="SJTU video ID from `video list`."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="SRT output path."),
    print_srt: bool = typer.Option(False, "--print", help="Print SRT text to stdout."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted subtitle JSON instead of SRT summary."),
) -> None:
    """Read subtitles for one video and optionally write an SRT file."""

    try:
        client, root = _make_video_client(token, base_url, data_dir)
        with client:
            body = client.get_video_subtitle(course_id, video_id)
        srt = subtitle_items_to_srt(body.preferred_items)
        if output is None:
            output = (
                root
                / "videos"
                / f"course-{course_id}"
                / f"{safe_path_segment(video_id, fallback='video')}.srt"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(srt, encoding="utf-8")
        if json_output:
            _print_video_json(
                {
                    "subtitle": body.model_dump(mode="json"),
                    "srt_path": str(output),
                    "srt_chars": len(srt),
                }
            )
            return
        if print_srt:
            typer.echo(srt)
        _render_subtitle_summary(body, srt, output)
    except AssistantError as exc:
        _exit_with_error(exc)


@video_app.command("ppt")
def video_ppt(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    video_id: str = typer.Argument(..., help="SJTU video ID from `video list`."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """Read PPT replay slice metadata for one video without downloading images."""

    try:
        client, _ = _make_video_client(token, base_url, data_dir)
        with client:
            slices = client.get_video_ppt_slices(course_id, video_id)
        if json_output:
            _print_video_json([item.model_dump(mode="json") for item in slices])
        else:
            _render_ppt_slices(slices)
    except AssistantError as exc:
        _exit_with_error(exc)


@video_app.command("ppt-export")
def video_ppt_export(
    course_id: int = typer.Argument(..., help="Canvas course ID."),
    video_id: str = typer.Argument(..., help="SJTU video ID from `video list`."),
    token: Optional[str] = typer.Option(None, "--token", help="Canvas token override."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Canvas base URL override."),
    data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Local data directory."),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory for PPT images and default PDF."),
    pdf_path: Optional[Path] = typer.Option(None, "--pdf-path", help="Optional PDF output path."),
    make_pdf: bool = typer.Option(True, "--pdf/--no-pdf", help="Assemble downloaded images into a PDF."),
    force: bool = typer.Option(False, "--force", help="Re-download existing images and regenerate PDF."),
    json_output: bool = typer.Option(False, "--json", help="Print redacted JSON."),
) -> None:
    """Download PPT replay images and optionally assemble them into a PDF."""

    try:
        client, root = _make_video_client(token, base_url, data_dir)
        with client:
            result = PPTAssetExporter(client, root).export_video_ppt(
                course_id,
                video_id,
                output_dir=output_dir,
                pdf_path=pdf_path,
                make_pdf=make_pdf,
                force=force,
            )
        if json_output:
            _print_video_json(result)
        else:
            _render_ppt_export_result(result)
    except AssistantError as exc:
        _exit_with_error(exc)


@app.command("video-status")
def video_status(
    course_id: Optional[int] = typer.Argument(None, help="Optional Canvas course ID."),
) -> None:
    """Show the SJTU video/subtitle integration surface."""

    info = video_index_status()
    console.print(f"[bold]Video status:[/bold] {info['status']}")
    if course_id is not None:
        console.print(
            f"Canvas sessionless LTI entry: /api/v1/courses/{course_id}/external_tools/sessionless_launch"
        )
    console.print_json(json.dumps(info, ensure_ascii=False))


@app.command("mcp")
def mcp() -> None:
    """Run the local MCP stdio server."""

    run_mcp_server()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
