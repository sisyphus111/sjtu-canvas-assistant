from sjtu_canvas_assistant.indexer import render_markdown_index
from sjtu_canvas_assistant.models import Assignment, Course, CourseIndex, DownloadRecord


def test_markdown_index_contains_download_and_assignment() -> None:
    index = CourseIndex(
        generated_at="2026-06-24T00:00:00+00:00",
        base_url="https://oc.sjtu.edu.cn",
        course=Course(id=1, name="Canvas Demo"),
        assignments=[Assignment(id=3, course_id=1, name="HW1", due_at="2026-07-01T00:00:00Z")],
        downloads=[
            DownloadRecord(
                file_id=2,
                canvas_path="slides/week1.pdf",
                file_name="week1.pdf",
                status="downloaded",
                size=100,
                relative_download_path="Canvas Demo [1]/slides/week1.pdf",
            )
        ],
    )

    markdown = render_markdown_index(index)

    assert "# Canvas Demo" in markdown
    assert "slides/week1.pdf" in markdown
    assert "HW1" in markdown
    assert "Video/subtitle/PPT" not in markdown
