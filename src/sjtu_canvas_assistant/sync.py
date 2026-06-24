"""Course synchronization and local file download orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .canvas_client import CanvasClient
from .exceptions import AssistantError, DownloadError
from .indexer import write_index
from .models import CanvasFile, CourseBundle, CourseIndex, DownloadRecord, Folder
from .path_utils import canvas_file_path, planned_file_path, unique_path_for_conflict
from .video_stub import video_index_stub


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class CourseSyncer:
    """Fetch course metadata, download files, and generate indexes."""

    def __init__(self, client: CanvasClient, data_dir: Path) -> None:
        self.client = client
        self.data_dir = data_dir

    def fetch_course_bundle(self, course_id: int) -> CourseBundle:
        course = self.client.get_course(course_id)
        folders = self.client.list_course_folders(course_id)
        files = self.client.list_course_files(course_id)
        modules = self.client.list_modules(course_id, include_items=True)
        assignments = self.client.list_assignments(course_id)
        return CourseBundle(
            generated_at=utc_now_iso(),
            base_url=self.client.base_url,
            course=course,
            folders=folders,
            files=files,
            modules=modules,
            assignments=assignments,
        )

    @staticmethod
    def _folder_map(folders: List[Folder]) -> Dict[int, Folder]:
        return {folder.id: folder for folder in folders}

    def _download_record(
        self,
        bundle: CourseBundle,
        file: CanvasFile,
        target: Optional[Path],
        status: str,
        message: str = "",
    ) -> DownloadRecord:
        folder_map = self._folder_map(bundle.folders)
        relative_path = None
        if target is not None:
            try:
                relative_path = target.relative_to(self.data_dir).as_posix()
            except ValueError:
                relative_path = target.as_posix()
        return DownloadRecord(
            file_id=file.id,
            canvas_path=canvas_file_path(file, folder_map),
            file_name=file.effective_name,
            status=status,
            size=file.size,
            updated_at=file.updated_at or file.modified_at,
            download_path=target,
            relative_download_path=relative_path,
            message=message,
        )

    def planned_downloads(self, bundle: CourseBundle, output_dir: Optional[Path] = None) -> List[Tuple[CanvasFile, Path]]:
        root_dir = output_dir or self.data_dir
        folder_map = self._folder_map(bundle.folders)
        return [
            (file, planned_file_path(root_dir, bundle.course, file, folder_map))
            for file in bundle.files
        ]

    def plan_download_records(
        self,
        bundle: CourseBundle,
        *,
        output_dir: Optional[Path] = None,
    ) -> List[DownloadRecord]:
        folder_map = self._folder_map(bundle.folders)
        root_dir = output_dir or self.data_dir
        used_targets: List[Path] = []
        records: List[DownloadRecord] = []

        for file in bundle.files:
            target = planned_file_path(root_dir, bundle.course, file, folder_map)
            status = "would_download"
            message = ""

            if target in used_targets:
                target = unique_path_for_conflict(target, used_targets, file.id)
                status = "would_download_renamed"
                message = "duplicate Canvas path"
            used_targets.append(target)

            if file.locked:
                status = "locked"
                message = "file is locked in Canvas"
            elif not file.url:
                status = "unavailable"
                message = "missing download URL"
            elif target.exists() and (not file.size or target.stat().st_size == file.size):
                status = "present"
                message = "already exists"
            elif target.exists():
                target = unique_path_for_conflict(target, used_targets, file.id)
                used_targets.append(target)
                status = "would_download_renamed"
                message = "local file with different size exists"

            records.append(self._download_record(bundle, file, target, status, message))

        return records

    def build_index_without_download(
        self,
        course_id: int,
        *,
        output_dir: Optional[Path] = None,
    ) -> Tuple[CourseIndex, Path, Path]:
        bundle = self.fetch_course_bundle(course_id)
        records = self.plan_download_records(bundle, output_dir=output_dir)
        index = self._make_index(bundle, records)
        return write_index(index, output_dir or self.data_dir)

    def download_course_files(
        self,
        course_id: int,
        *,
        output_dir: Optional[Path] = None,
        skip_existing: bool = True,
    ) -> Tuple[CourseIndex, Path, Path]:
        bundle = self.fetch_course_bundle(course_id)
        folder_map = self._folder_map(bundle.folders)
        root_dir = output_dir or self.data_dir
        used_targets: List[Path] = []
        records: List[DownloadRecord] = []

        for file in bundle.files:
            target = planned_file_path(root_dir, bundle.course, file, folder_map)
            if target in used_targets:
                target = unique_path_for_conflict(target, used_targets, file.id)
            used_targets.append(target)

            if skip_existing and target.exists() and (not file.size or target.stat().st_size == file.size):
                records.append(self._download_record(bundle, file, target, "skipped", "already exists"))
                continue

            if file.locked:
                records.append(self._download_record(bundle, file, target, "failed", "file is locked in Canvas"))
                continue

            try:
                if target.exists() and file.size and target.stat().st_size != file.size:
                    target = unique_path_for_conflict(target, used_targets, file.id)
                    used_targets.append(target)
                self.client.download_file(file, target)
            except (AssistantError, OSError) as exc:
                records.append(self._download_record(bundle, file, target, "failed", str(exc)))
            else:
                records.append(self._download_record(bundle, file, target, "downloaded"))

        index = self._make_index(bundle, records)
        return write_index(index, root_dir)

    def _make_index(self, bundle: CourseBundle, records: List[DownloadRecord]) -> CourseIndex:
        return CourseIndex(
            generated_at=utc_now_iso(),
            base_url=bundle.base_url,
            course=bundle.course,
            folders=bundle.folders,
            files=bundle.files,
            modules=bundle.modules,
            assignments=bundle.assignments,
            downloads=records,
            video=video_index_stub(),
            mcp={
                "status": "planned",
                "note": "The CLI services are intentionally split into reusable modules so a future MCP server can expose the same methods.",
                "candidate_tools": [
                    "get_current_user",
                    "list_courses",
                    "list_course_files",
                    "list_course_folders",
                    "list_course_modules",
                    "list_course_assignments",
                    "download_course_files",
                    "generate_course_index",
                ],
            },
        )
