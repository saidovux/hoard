"""Real FFmpeg frame extraction, including failure and no-upscale paths."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hoard import video_frames
from hoard.video_frames import FrameExtractionError, extract_frames, probe_video

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)


def make_video(
    path: Path,
    *,
    size: str = "320x240",
    duration: str = "3",
    source: str = "testsrc2",
) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"{source}=size={size}:rate=10",
            "-t",
            duration,
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


async def probe_dimensions(path: Path) -> tuple[int, int]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await process.communicate()
    width, height = stdout.decode().strip().split(",")
    return int(width), int(height)


@needs_ffmpeg
async def test_extracts_three_ordered_frames_at_actual_relative_positions(tmp_path):
    video = tmp_path / "video.mp4"
    make_video(video)
    original = video.read_bytes()
    output = tmp_path / "frames"

    result = await extract_frames(video, output)

    assert abs(result.duration - 3.0) < 0.05
    assert [frame.position for frame in result.frames] == [1, 2, 3]
    assert [round(frame.timestamp_seconds, 1) for frame in result.frames] == [0.6, 1.5, 2.4]
    assert len({frame.sha256 for frame in result.frames}) == 3
    assert all(frame.path.is_file() for frame in result.frames)
    assert all(frame.path.read_bytes().startswith(b"\xff\xd8\xff") for frame in result.frames)
    assert video.read_bytes() == original
    assert await probe_dimensions(result.frames[0].path) == (320, 240)


@needs_ffmpeg
async def test_large_video_is_downscaled_proportionally_without_upscale(tmp_path):
    video = tmp_path / "large.mp4"
    make_video(video, size="1920x1080", duration="1")
    result = await extract_frames(video, tmp_path / "frames")
    assert await probe_dimensions(result.frames[0].path) == (1280, 720)


@needs_ffmpeg
async def test_source_duration_is_not_hardcoded(tmp_path):
    video = tmp_path / "ten.mp4"
    make_video(video, duration="10")
    probe = await probe_video(video)
    assert abs(probe["duration"] - 10.0) < 0.05
    result = await extract_frames(video, tmp_path / "frames")
    assert [round(frame.timestamp_seconds, 1) for frame in result.frames] == [2.0, 5.0, 8.0]


@needs_ffmpeg
async def test_identical_real_frames_are_not_faked_as_three(tmp_path):
    video = tmp_path / "black.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=320x240:rate=10",
            "-t",
            "3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-y",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(FrameExtractionError) as caught:
        await extract_frames(video, tmp_path / "frames")
    assert caught.value.code == "insufficient_distinct_frames"


@needs_ffmpeg
async def test_corrupt_or_too_short_video_has_bounded_diagnostics(tmp_path):
    corrupt = tmp_path / "broken.mp4"
    corrupt.write_bytes(b"not an mp4")
    with pytest.raises(FrameExtractionError) as caught:
        await probe_video(corrupt)
    assert caught.value.code in {"invalid_video", "no_video_stream"}
    assert len(caught.value.detail) <= 500

    short = tmp_path / "short.mp4"
    make_video(short, size="160x120", duration="0.1")
    with pytest.raises(FrameExtractionError) as caught:
        await extract_frames(short, tmp_path / "frames")
    assert caught.value.code in {"decode_failed", "insufficient_distinct_frames", "too_short"}


async def test_missing_ffmpeg_is_reported_without_touching_source(tmp_path, monkeypatch):
    real_which = shutil.which

    def fake_which(name):
        if name == "ffmpeg":
            return None
        return real_which(name)

    monkeypatch.setattr(video_frames.shutil, "which", fake_which)
    if real_which("ffprobe") is None:
        pytest.skip("ffprobe is not installed")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic")
    with pytest.raises(FrameExtractionError) as caught:
        await extract_frames(video, tmp_path / "frames")
    assert caught.value.code in {"ffmpeg_missing", "invalid_video", "no_video_stream"}
    assert video.read_bytes() == b"synthetic"
    assert not (tmp_path / "frames" / "frame_01.jpg").exists()


async def test_ffmpeg_uses_one_decode_and_filter_thread(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    async def fake_run(arguments, *, timeout, code):
        calls.append(list(arguments))
        Path(arguments[-1]).write_bytes(b"\xff\xd8\xff" + b"x" * 128)
        return 0, b"", b""

    monkeypatch.setattr(video_frames, "_run", fake_run)
    await video_frames._extract_one(
        "ffmpeg",
        tmp_path / "source.mp4",
        tmp_path / "frame.jpg",
        1.5,
        timeout=10,
    )

    command = calls[0]
    input_index = command.index("-i")
    assert command[command.index("-threads") + 1] == "1"
    assert command[command.index("-filter_threads") + 1] == "1"
    assert command.index("-threads") < input_index
    assert command.index("-filter_threads") < input_index
    assert command[input_index + 1] == str(tmp_path / "source.mp4")


async def test_cancelling_subprocess_waits_until_process_is_reaped(monkeypatch):
    original_create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def recording_create(*arguments, **options):
        process = await original_create(*arguments, **options)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(video_frames.asyncio, "create_subprocess_exec", recording_create)
    task = asyncio.create_task(
        video_frames._run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=60,
            code="test",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(processes) == 1
    assert processes[0].returncode is not None
