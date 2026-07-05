from pathlib import Path

from sjtu_canvas_assistant.models import (
    Assignment,
    CanvasFile,
    Course,
    CourseBundle,
    Folder,
    Submission,
    SubmissionAttachment,
    SubmissionAttempt,
)
from sjtu_canvas_assistant.sync import CourseSyncer


def test_plan_download_records_marks_existing_and_renamed_paths(tmp_path: Path) -> None:
    course = Course(id=42, name="软件测试")
    folder = Folder(id=10, name="course files", full_name="course files")
    existing = CanvasFile(
        id=1,
        folder_id=10,
        display_name="slides.pdf",
        url="https://oc.sjtu.edu.cn/files/1/download",
        size=3,
    )
    conflicting = CanvasFile(
        id=2,
        folder_id=10,
        display_name="report.pdf",
        url="https://oc.sjtu.edu.cn/files/2/download",
        size=10,
    )
    bundle = CourseBundle(
        generated_at="now",
        base_url="https://oc.sjtu.edu.cn",
        course=course,
        folders=[folder],
        files=[existing, conflicting],
    )

    course_dir = tmp_path / "软件测试 [42]"
    course_dir.mkdir()
    (course_dir / "slides.pdf").write_bytes(b"abc")
    (course_dir / "report.pdf").write_bytes(b"different-size")

    syncer = CourseSyncer(client=None, data_dir=tmp_path)  # type: ignore[arg-type]
    records = syncer.plan_download_records(bundle)

    assert records[0].status == "present"
    assert records[1].status == "would_download_renamed"
    assert records[1].download_path is not None
    assert records[1].download_path.name == "report__canvas_2.pdf"


class FakeSubmissionClient:
    base_url = "https://oc.sjtu.edu.cn"

    def __init__(self) -> None:
        self.downloads = 0
        self.course = Course(id=42, name="软件测试")
        self.assignment = Assignment(id=7, name="期末报告")
        self.submission = Submission(
            id=99,
            assignment_id=7,
            submitted_at="2026-06-01T00:00:00Z",
            attempt=2,
            attachments=[
                SubmissionAttachment(
                    id=12,
                    display_name="answer-v2.pdf",
                    url="https://oc.sjtu.edu.cn/files/12/download?verifier=secret",
                    size=3,
                )
            ],
            submission_history=[
                SubmissionAttempt(
                    id=98,
                    attempt=1,
                    submitted_at="2026-05-31T00:00:00Z",
                    attachments=[
                        SubmissionAttachment(
                            id=11,
                            display_name="answer.pdf",
                            url="https://oc.sjtu.edu.cn/files/11/download?verifier=secret",
                            size=3,
                        )
                    ],
                ),
                SubmissionAttempt(
                    id=99,
                    attempt=2,
                    submitted_at="2026-06-01T00:00:00Z",
                    attachments=[
                        SubmissionAttachment(
                            id=12,
                            display_name="answer-v2.pdf",
                            url="https://oc.sjtu.edu.cn/files/12/download?verifier=secret",
                            size=3,
                        )
                    ],
                ),
            ],
        )

    def get_course(self, course_id: int) -> Course:
        assert course_id == 42
        return self.course

    def get_assignment(self, course_id: int, assignment_id: int) -> Assignment:
        assert course_id == 42
        assert assignment_id == 7
        return self.assignment

    def get_my_submission(self, course_id: int, assignment_id: int) -> Submission:
        assert course_id == 42
        assert assignment_id == 7
        return self.submission

    def list_my_submissions(self, course_id: int):
        assert course_id == 42
        return [(self.assignment, self.submission)]

    def download_submission_attachment(self, attachment, destination: Path, *, submission_id=None) -> None:
        assert submission_id == 99
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"abc")


def test_download_my_submission_files_preserves_attempts_and_skips_existing(tmp_path: Path) -> None:
    client = FakeSubmissionClient()
    syncer = CourseSyncer(client=client, data_dir=tmp_path)  # type: ignore[arg-type]

    result = syncer.download_my_submission_files(42, assignment_id=7)

    assert result.summary == {"downloaded": 2}
    assert client.downloads == 2
    assert result.downloads[0].relative_download_path == (
        "软件测试 [42]/submissions/期末报告 [7]/attempt-1/answer.pdf"
    )
    assert result.downloads[1].relative_download_path == (
        "软件测试 [42]/submissions/期末报告 [7]/attempt-2/answer-v2.pdf"
    )
    assert all(record.download_path and record.download_path.exists() for record in result.downloads)

    second = syncer.download_my_submission_files(42, assignment_id=7)

    assert second.summary == {"skipped": 2}
    assert client.downloads == 2


def test_download_my_submission_files_latest_only_uses_top_level_attachments(tmp_path: Path) -> None:
    client = FakeSubmissionClient()
    syncer = CourseSyncer(client=client, data_dir=tmp_path)  # type: ignore[arg-type]

    result = syncer.download_my_submission_files(42, assignment_id=7, include_history=False)

    assert result.summary == {"downloaded": 1}
    assert result.downloads[0].attempt == 2
    assert result.downloads[0].relative_download_path == (
        "软件测试 [42]/submissions/期末报告 [7]/latest/answer-v2.pdf"
    )
