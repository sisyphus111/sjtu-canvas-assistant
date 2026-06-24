from pathlib import Path

from sjtu_canvas_assistant.models import CanvasFile, Course, CourseBundle, Folder
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
