import pytest

from hoard.integrations.media import validate_media
from hoard.integrations.rclone import validate_upload


def test_rclone_accepts_single_file_or_folder(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"test")
    assert validate_upload(str(source), "cloud:archive/clip.mp4") == (
        str(source),
        "cloud:archive/clip.mp4",
    )
    assert validate_upload(str(tmp_path), "cloud:folder") == (str(tmp_path), "cloud:folder")


@pytest.mark.parametrize(
    "remote", ["", "https://example.com", "-bad:path", "x:\nunsafe", "no_colon"]
)
def test_rclone_rejects_invalid_remote(tmp_path, remote):
    with pytest.raises(ValueError):
        validate_upload(str(tmp_path), remote)


def test_media_validates_web_urls(tmp_path):
    assert validate_media("https://example.com/video", str(tmp_path))[0] == "https://example.com/video"
    for url in ["file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.com/foo", ""]:
        with pytest.raises(ValueError):
            validate_media(url, str(tmp_path))
