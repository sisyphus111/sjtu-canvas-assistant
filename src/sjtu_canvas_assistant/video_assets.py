"""Local export helpers for SJTU video PPT replay assets."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from .exceptions import VideoIntegrationError
from .models import PPTExportResult, PPTImageRecord, VideoPPTSlice
from .path_utils import safe_path_segment

IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _offset_name(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds)) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}-{minutes:02}-{seconds:02}"


def _extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ""


def _extension_from_content_type(content_type: str) -> str:
    return IMAGE_EXTENSIONS.get(content_type.lower().split(";", 1)[0].strip(), "")


def _slice_prefix(index: int, item: VideoPPTSlice) -> str:
    return f"{index:03d}_{_offset_name(item.create_sec)}"


def _existing_image_for_prefix(image_dir: Path, prefix: str) -> Optional[Path]:
    for candidate in sorted(image_dir.glob(f"{prefix}.*")):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _image_to_rgb(path: Path) -> Image.Image:
    try:
        image = Image.open(path)
        image.load()
    except (OSError, UnidentifiedImageError) as exc:
        raise VideoIntegrationError(f"Cannot read PPT image for PDF: {path}") from exc

    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image.close()
        return background
    converted = image.convert("RGB")
    image.close()
    return converted


def images_to_pdf(image_paths: Iterable[Path], pdf_path: Path) -> None:
    """Assemble image files into a single PDF using their native dimensions."""

    paths = [path for path in image_paths if path.exists() and path.stat().st_size > 0]
    if not paths:
        raise VideoIntegrationError("No PPT images are available for PDF assembly.")

    images = [_image_to_rgb(path) for path in paths]
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pdf_path.with_name(pdf_path.name + ".part")
    try:
        first, rest = images[0], images[1:]
        first.save(tmp_path, "PDF", resolution=100.0, save_all=True, append_images=rest)
        tmp_path.replace(pdf_path)
    finally:
        for image in images:
            image.close()
        if tmp_path.exists():
            tmp_path.unlink()


class PPTAssetExporter:
    """Download PPT replay images and optionally assemble them into a PDF."""

    def __init__(self, video_client: Any, data_dir: Path) -> None:
        self.video_client = video_client
        self.data_dir = data_dir

    def default_output_dir(self, course_id: int, video_id: str) -> Path:
        return (
            self.data_dir
            / "videos"
            / f"course-{course_id}"
            / safe_path_segment(video_id, fallback="video")
            / "ppt"
        )

    def default_pdf_path(self, image_dir: Path) -> Path:
        return image_dir / "ppt.pdf"

    def export_video_ppt(
        self,
        course_id: int,
        video_id: str,
        *,
        output_dir: Optional[Path] = None,
        pdf_path: Optional[Path] = None,
        make_pdf: bool = True,
        force: bool = False,
    ) -> PPTExportResult:
        slices = self.video_client.get_video_ppt_slices(course_id, video_id)
        image_dir = (output_dir or self.default_output_dir(course_id, video_id)).expanduser()
        image_dir.mkdir(parents=True, exist_ok=True)
        resolved_pdf_path = (pdf_path.expanduser() if pdf_path else self.default_pdf_path(image_dir)) if make_pdf else None

        records = [
            self._download_slice_image(index, item, image_dir=image_dir, force=force)
            for index, item in enumerate(slices, start=1)
        ]
        pdf_status = ""
        if make_pdf and resolved_pdf_path is not None:
            image_paths = [
                record.image_path
                for record in records
                if record.image_path is not None
                and record.image_path.exists()
                and record.status in {"downloaded", "skipped"}
            ]
            if not image_paths:
                pdf_status = "failed"
            elif (
                resolved_pdf_path.exists()
                and not force
                and all(record.status == "skipped" for record in records if record.image_path is not None)
            ):
                pdf_status = "skipped"
            else:
                images_to_pdf(image_paths, resolved_pdf_path)
                pdf_status = "created"

        counts = Counter(record.status for record in records)
        if pdf_status:
            counts[f"pdf_{pdf_status}"] += 1
        return PPTExportResult(
            course_id=course_id,
            video_id=video_id,
            image_dir=image_dir,
            relative_image_dir=_relative_to_root(image_dir, self.data_dir),
            pdf_path=resolved_pdf_path,
            relative_pdf_path=(
                _relative_to_root(resolved_pdf_path, self.data_dir)
                if resolved_pdf_path is not None
                else None
            ),
            pdf_status=pdf_status,
            slices_count=len(slices),
            summary=dict(sorted(counts.items())),
            images=records,
        )

    def _download_slice_image(
        self,
        index: int,
        item: VideoPPTSlice,
        *,
        image_dir: Path,
        force: bool,
    ) -> PPTImageRecord:
        prefix = _slice_prefix(index, item)
        if not item.ppt_img_url:
            return PPTImageRecord(
                slice_index=index,
                offset_ms=item.create_sec,
                status="unavailable",
                message="missing PPT image URL",
            )

        existing = _existing_image_for_prefix(image_dir, prefix)
        if existing is not None and not force:
            return PPTImageRecord(
                slice_index=index,
                offset_ms=item.create_sec,
                status="skipped",
                image_path=existing,
                relative_image_path=_relative_to_root(existing, self.data_dir),
                size=existing.stat().st_size,
                message="already exists",
            )

        try:
            content, content_type = self.video_client.download_ppt_image(item.ppt_img_url)
        except VideoIntegrationError as exc:
            return PPTImageRecord(
                slice_index=index,
                offset_ms=item.create_sec,
                status="failed",
                image_path=existing,
                relative_image_path=(
                    _relative_to_root(existing, self.data_dir) if existing is not None else None
                ),
                size=existing.stat().st_size if existing is not None else 0,
                message=str(exc),
            )

        extension = (
            _extension_from_content_type(content_type)
            or _extension_from_url(item.ppt_img_url)
            or ".jpg"
        )
        target = image_dir / f"{prefix}{extension}"
        tmp_path = target.with_name(target.name + ".part")
        tmp_path.write_bytes(content)
        tmp_path.replace(target)
        return PPTImageRecord(
            slice_index=index,
            offset_ms=item.create_sec,
            status="downloaded",
            image_path=target,
            relative_image_path=_relative_to_root(target, self.data_dir),
            size=target.stat().st_size,
            content_type=content_type,
        )
