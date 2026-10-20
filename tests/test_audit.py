"""Regression tests for command parsing and protecting local file destinations."""
import os
import subprocess
import sys

import pytest

from hoard.integrations.media import validate_media
from hoard.integrations.rclone import validate_upload
from hoard.models import Post
from hoard.storage import Store


def test_empty_rclone_source_does_not_upload_cwd():
    with pytest.raises(ValueError, match="Укажи локальный"):
        validate_upload("", "myremote:private")


def test_empty_download_destination_is_not_cwd():
    with pytest.raises(ValueError, match="Укажи каталог"):
        validate_media("https://example.com/watch?v=1", " ")


def test_empty_archive_destination_is_not_cwd(tmp_path):
    store = Store(tmp_path / "hoard.sqlite3")
    try:
        with pytest.raises(ValueError, match="Укажи каталог"):
            store.add_source(Post("validname", 1), "")
    finally:
        store.close()


def test_json_option_after_command(tmp_path):
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    result = subprocess.run([sys.executable, "-m", "hoard", "status", "--json"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '"running": false' in result.stdout.lower()
