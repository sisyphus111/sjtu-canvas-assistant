"""Course synchronization and local file download orchestration."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .canvas_client import CanvasClient
from .exceptions import AssistantError
from .indexer import write_index
from .models import (
    Assignment,
    CanvasFile,
    CourseBundle,
    CourseIndex,
    DownloadRecord,
    Folder,
    MySubmission,
    Submission,
    SubmissionAttachment,
    SubmissionAttachmentDownloadRecord,
    SubmissionAttempt,
    SubmissionDownloadResult,
)
from .path_utils import (
    canvas_file_path,
    course_dir_name,
    planned_file_path,
    safe_path_segment,
    unique_path_for_conflict,
)
from .video_client import video_index_status


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

    def fetch_my_submissions(
        self,
        course_id: int,
        *,
        submitted_only: bool = False,
    ) -> List[MySubmission]:
        submissions = [
            MySubmission(assignment=assignment, submission=submission)
            for assignment, submission in self.client.list_my_submissions(course_id)
        ]
        if submitted_only:
            return [
                record
                for record in submissions
                if record.submission.submitted_at
                or record.submission.attachments
                or any(attempt.attachments for attempt in record.submission.submission_history)
            ]
        return submissions

    def fetch_my_submission(self, course_id: int, assignment_id: int) -> MySubmission:
        assignment = self.client.get_assignment(course_id, assignment_id)
        submission = self.client.get_my_submission(course_id, assignment_id)
        return MySubmission(assignment=assignment, submission=submission)

    def download_my_submission_files(
        self,
        course_id: int,
        *,
        assignment_id: Optional[int] = None,
        output_dir: Optional[Path] = None,
        include_history: bool = True,
        skip_existing: bool = True,
    ) -> SubmissionDownloadResult:
        course = self.client.get_course(course_id)
        if assignment_id is None:
            submissions = self.fetch_my_submissions(course_id, submitted_only=False)
        else:
            submissions = [self.fetch_my_submission(course_id, assignment_id)]

        root_dir = output_dir or self.data_dir
        base_dir = root_dir / course_dir_name(course) / "submissions"
        records: List[SubmissionAttachmentDownloadRecord] = []
        used_targets: List[Path] = []

        for item in submissions:
            assignment = item.assignment
            submission = item.submission
            for attempt, attachment in _submission_attachment_entries(
                submission, include_history=include_history
            ):
                target = (
                    base_dir
                    / safe_path_segment(
                        f"{assignment.name or 'assignment'} [{assignment.id}]",
                        fallback=f"assignment-{assignment.id}",
                    )
                    / _attempt_dir_name(attempt)
                    / safe_path_segment(
                        attachment.effective_name,
                        fallback=f"attachment-{attachment.id}",
                    )
                )
                if target in used_targets:
                    target = unique_path_for_conflict(target, used_targets, attachment.id)
                used_targets.append(target)

                if skip_existing and target.exists() and (
                    not attachment.size or target.stat().st_size == attachment.size
                ):
                    records.append(
                        self._submission_download_record(
                            assignment,
                            submission,
                            attempt,
                            attachment,
                            target,
                            "skipped",
                            "already exists",
                        )
                    )
                    continue
                if target.exists() and attachment.size and target.stat().st_size != attachment.size:
                    target = unique_path_for_conflict(target, used_targets, attachment.id)
                    used_targets.append(target)

                try:
                    self.client.download_submission_attachment(
                        attachment, target, submission_id=submission.id
                    )
                except (AssistantError, OSError) as exc:
                    records.append(
                        self._submission_download_record(
                            assignment,
                            submission,
                            attempt,
                            attachment,
                            target,
                            "failed",
                            str(exc),
                        )
                    )
                else:
                    records.append(
                        self._submission_download_record(
                            assignment,
                            submission,
                            attempt,
                            attachment,
                            target,
                            "downloaded",
                        )
                    )

        return SubmissionDownloadResult(
            generated_at=utc_now_iso(),
            base_url=self.client.base_url,
            course=course,
            submissions=submissions,
            downloads=records,
            summary=dict(sorted(Counter(record.status for record in records).items())),
        )

    def _submission_download_record(
        self,
        assignment: Assignment,
        submission: Submission,
        attempt: Optional[SubmissionAttempt],
        attachment: SubmissionAttachment,
        target: Optional[Path],
        status: str,
        message: str = "",
    ) -> SubmissionAttachmentDownloadRecord:
        relative_path = None
        if target is not None:
            try:
                relative_path = target.relative_to(self.data_dir).as_posix()
            except ValueError:
                relative_path = target.as_posix()
        return SubmissionAttachmentDownloadRecord(
            assignment_id=assignment.id,
            assignment_name=assignment.name,
            submission_id=submission.id,
            attempt=attempt.attempt if attempt else submission.attempt,
            submitted_at=(
                attempt.submitted_at
                if attempt and attempt.submitted_at
                else submission.submitted_at
            ),
            attachment_id=attachment.id,
            file_name=attachment.effective_name,
            status=status,
            size=attachment.size,
            content_type=attachment.content_type,
            download_path=target,
            relative_download_path=relative_path,
            message=message,
        )

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
            video=video_index_status(),
            mcp={
                "status": "implemented",
                "note": "The local MCP stdio server exposes the same read/download/index methods as the CLI.",
                "candidate_tools": [
                    "get_current_user",
                    "list_courses",
                    "list_course_files",
                    "list_course_folders",
                    "list_course_modules",
                    "list_course_assignments",
                    "list_my_submissions",
                    "get_my_submission",
                    "download_my_submission_files",
                    "download_course_files",
                    "generate_course_index",
                    "list_course_videos",
                    "get_video_info",
                    "get_video_subtitle",
                    "get_video_ppt_slices",
                ],
            },
        )


def _attempt_dir_name(attempt: Optional[SubmissionAttempt]) -> str:
    if attempt is None or attempt.attempt is None:
        return "latest"
    return safe_path_segment(f"attempt-{attempt.attempt}", fallback="attempt")


def _submission_attachment_entries(
    submission: Submission,
    *,
    include_history: bool,
) -> List[Tuple[Optional[SubmissionAttempt], SubmissionAttachment]]:
    entries: List[Tuple[Optional[SubmissionAttempt], SubmissionAttachment]] = []
    if include_history:
        for attempt in sorted(
            submission.submission_history,
            key=lambda item: item.attempt if item.attempt is not None else -1,
        ):
            entries.extend((attempt, attachment) for attachment in attempt.attachments)

    if not entries:
        entries.extend((None, attachment) for attachment in submission.attachments)
    else:
        seen = {
            (
                attempt.attempt if attempt else None,
                attachment.id,
            )
            for attempt, attachment in entries
        }
        for attachment in submission.attachments:
            key = (submission.attempt, attachment.id)
            if key not in seen:
                entries.append((None, attachment))

    return entries
