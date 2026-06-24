from pathlib import Path

from sjtu_canvas_assistant.models import CanvasFile, Course, Folder
from sjtu_canvas_assistant.path_utils import (
    course_dir_name,
    folder_relative_parts,
    planned_file_path,
    safe_path_segment,
)


def test_safe_path_segment_removes_cross_platform_invalid_chars() -> None:
    assert safe_path_segment(' A/B:C*D? "E" ') == 'A_B_C_D_ _E_'
    assert safe_path_segment("...") == "_"
    assert safe_path_segment("CON") == "CON_"


def test_canvas_folder_prefix_is_removed() -> None:
    folder = Folder(id=1, name="Week 1", full_name="course files/课件/Week 1")
    assert folder_relative_parts(folder) == ["课件", "Week 1"]


def test_planned_file_path_preserves_course_structure(tmp_path: Path) -> None:
    course = Course(id=42, name="测试课程")
    folder = Folder(id=10, name="Week 1", full_name="course files/课件/Week 1")
    file = CanvasFile(id=7, folder_id=10, display_name="intro:slides?.pdf", size=10)

    path = planned_file_path(tmp_path, course, file, {10: folder})

    assert path == tmp_path / course_dir_name(course) / "课件" / "Week 1" / "intro_slides_.pdf"
