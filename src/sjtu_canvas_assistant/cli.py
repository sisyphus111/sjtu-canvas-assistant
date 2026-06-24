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
from .models import Assignment, CanvasFile, Course, CourseBundle, DownloadRecord, Folder, Module
from .sync import CourseSyncer
from .video_stub import video_index_stub

app = typer.Typer(
    name="sjtu-canvas",
    no_args_is_help=True,
    help="Local CLI framework for SJTU Canvas courses, files, assignments, and indexes.",
)
console = Console()
FETCH_STATUSES = {"would_download", "would_download_renamed"}


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


def _print_json(value: object) -> None:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
    else:
        payload = value
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


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


@app.command("video-status")
def video_status(
    course_id: Optional[int] = typer.Argument(None, help="Optional Canvas course ID."),
) -> None:
    """Show the planned SJTU video/subtitle integration surface."""

    info = video_index_stub()
    console.print(f"[bold]Video status:[/bold] {info['status']}")
    console.print(info["note"])
    if course_id is not None:
        console.print(
            f"Planned LTI entry: /courses/{course_id}/external_tools/8329"
        )
    console.print_json(json.dumps(info, ensure_ascii=False))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
