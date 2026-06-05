from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import imageio_ffmpeg


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_video_first_frame(video_path: Path) -> str:
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-loglevel", "error",
            "-i", str(video_path),
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(
            f"ffmpeg failed to extract first frame from {video_path}: {result.stderr.decode(errors='replace')}"
        )
    return sha256_bytes(result.stdout)
