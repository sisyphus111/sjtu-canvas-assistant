from io import BytesIO

from PIL import Image

from sjtu_canvas_assistant.models import VideoPPTSlice
from sjtu_canvas_assistant.video_assets import PPTAssetExporter, images_to_pdf


def _png_bytes(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (16, 9), color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeVideoClient:
    def __init__(self) -> None:
        self.downloads = 0

    def get_video_ppt_slices(self, course_id: int, video_id: str):
        assert course_id == 42
        assert video_id == "video/1"
        return [
            VideoPPTSlice(createSec=0, pptImgUrl="https://example.test/1.png"),
            VideoPPTSlice(createSec=1500, pptImgUrl="https://example.test/2.png"),
        ]

    def download_ppt_image(self, image_url: str):
        self.downloads += 1
        return _png_bytes("red" if image_url.endswith("1.png") else "blue"), "image/png"


def test_ppt_asset_exporter_downloads_images_and_assembles_pdf(tmp_path) -> None:
    client = FakeVideoClient()
    result = PPTAssetExporter(client, tmp_path).export_video_ppt(
        42,
        "video/1",
        make_pdf=True,
    )

    assert result.slices_count == 2
    assert result.summary["downloaded"] == 2
    assert result.summary["pdf_created"] == 1
    assert result.pdf_path is not None
    assert result.pdf_path.exists()
    assert result.pdf_path.read_bytes().startswith(b"%PDF")
    assert all(record.image_path and record.image_path.exists() for record in result.images)
    assert client.downloads == 2

    second = PPTAssetExporter(client, tmp_path).export_video_ppt(
        42,
        "video/1",
        make_pdf=True,
    )

    assert second.summary["skipped"] == 2
    assert second.summary["pdf_skipped"] == 1
    assert client.downloads == 2


def test_images_to_pdf_uses_existing_image_files(tmp_path) -> None:
    image_path = tmp_path / "slide.png"
    image_path.write_bytes(_png_bytes("green"))
    pdf_path = tmp_path / "slides.pdf"

    images_to_pdf([image_path], pdf_path)

    assert pdf_path.read_bytes().startswith(b"%PDF")
