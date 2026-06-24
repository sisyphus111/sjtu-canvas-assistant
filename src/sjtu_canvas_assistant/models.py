"""Pydantic models for the Canvas API surface used by v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CanvasModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Teacher(CanvasModel):
    id: Optional[int] = None
    display_name: str = ""
    avatar_image_url: str = ""
    html_url: str = ""


class Term(CanvasModel):
    id: Optional[int] = None
    name: str = ""
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    workflow_state: str = ""


class Enrollment(CanvasModel):
    type: str = ""
    role: str = ""
    enrollment_state: str = ""


class Course(CanvasModel):
    id: int
    uuid: str = ""
    name: str = ""
    course_code: str = ""
    enrollments: List[Enrollment] = Field(default_factory=list)
    access_restricted_by_date: Optional[bool] = None
    teachers: List[Teacher] = Field(default_factory=list)
    term: Optional[Term] = None
    workflow_state: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.course_code or f"course-{self.id}"


class User(CanvasModel):
    id: int
    name: str = ""
    sortable_name: str = ""
    short_name: str = ""
    login_id: str = ""
    email: Optional[str] = None


class Folder(CanvasModel):
    id: int
    name: str = ""
    full_name: str = ""
    parent_folder_id: Optional[int] = None
    locked: bool = False
    files_count: int = 0
    folders_count: int = 0


class CanvasFile(CanvasModel):
    id: int
    uuid: str = ""
    folder_id: Optional[int] = None
    display_name: str = ""
    filename: str = ""
    url: str = ""
    size: int = 0
    locked: bool = False
    hidden: Optional[bool] = None
    mime_class: str = ""
    content_type: str = Field(default="", alias="content-type")
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    modified_at: Optional[str] = None

    @property
    def effective_name(self) -> str:
        return self.display_name or self.filename or f"file-{self.id}"


class CompletionRequirement(CanvasModel):
    type: str = ""
    min_score: Optional[float] = None
    completed: Optional[bool] = None


class ContentDetails(CanvasModel):
    points_possible: Optional[float] = None
    due_at: Optional[str] = None
    unlock_at: Optional[str] = None
    lock_at: Optional[str] = None


class ModuleItem(CanvasModel):
    id: int
    module_id: Optional[int] = None
    position: int = 0
    title: str = ""
    indent: int = 0
    type: str = Field(default="", alias="type")
    content_id: Optional[int] = None
    html_url: str = ""
    url: Optional[str] = None
    page_url: Optional[str] = None
    external_url: Optional[str] = None
    new_tab: Optional[bool] = None
    completion_requirement: Optional[CompletionRequirement] = None
    content_details: Optional[ContentDetails] = None
    published: Optional[bool] = None


class Module(CanvasModel):
    id: int
    position: int = 0
    name: str = ""
    unlock_at: Optional[str] = None
    require_sequential_progress: bool = False
    prerequisite_module_ids: List[int] = Field(default_factory=list)
    items_count: int = 0
    items_url: str = ""
    items: List[ModuleItem] = Field(default_factory=list)
    state: Optional[str] = None
    completed_at: Optional[str] = None
    published: Optional[bool] = None


class AssignmentDate(CanvasModel):
    id: Optional[int] = None
    base: bool = False
    title: str = ""
    due_at: Optional[str] = None
    unlock_at: Optional[str] = None
    lock_at: Optional[str] = None


class Assignment(CanvasModel):
    id: int
    course_id: Optional[int] = None
    name: str = ""
    description: Optional[str] = None
    due_at: Optional[str] = None
    unlock_at: Optional[str] = None
    lock_at: Optional[str] = None
    points_possible: Optional[float] = None
    html_url: str = ""
    submission_types: List[str] = Field(default_factory=list)
    allowed_extensions: List[str] = Field(default_factory=list)
    has_submitted_submissions: Optional[bool] = None
    published: Optional[bool] = None
    submissions_download_url: str = ""
    all_dates: List[AssignmentDate] = Field(default_factory=list)
    grading_type: str = ""


class DownloadRecord(BaseModel):
    file_id: int
    canvas_path: str
    file_name: str
    status: str
    size: int = 0
    updated_at: Optional[str] = None
    download_path: Optional[Path] = None
    relative_download_path: Optional[str] = None
    message: str = ""


class CourseBundle(BaseModel):
    generated_at: str
    base_url: str
    course: Course
    folders: List[Folder] = Field(default_factory=list)
    files: List[CanvasFile] = Field(default_factory=list)
    modules: List[Module] = Field(default_factory=list)
    assignments: List[Assignment] = Field(default_factory=list)


class CourseIndex(BaseModel):
    generated_at: str
    base_url: str
    course: Course
    folders: List[Folder] = Field(default_factory=list)
    files: List[CanvasFile] = Field(default_factory=list)
    modules: List[Module] = Field(default_factory=list)
    assignments: List[Assignment] = Field(default_factory=list)
    downloads: List[DownloadRecord] = Field(default_factory=list)
    video: Dict[str, Any] = Field(default_factory=dict)
    mcp: Dict[str, Any] = Field(default_factory=dict)
