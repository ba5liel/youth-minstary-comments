"""Image discovery and normalisation (phone photos: EXIF rotation, HEIC, huge resolutions)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}


def find_images(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rotations(path: Path, max_side: int = 768) -> list[tuple[int, bytes]]:
    """The photo turned 0/90/180/270 degrees clockwise, small, for orientation detection."""
    base = Image.open(io.BytesIO(to_jpeg(path, max_side, 80)))
    out = []
    for rot in (0, 90, 180, 270):
        buf = io.BytesIO()
        base.rotate(-rot, expand=True).save(buf, "JPEG", quality=80)
        out.append((rot, buf.getvalue()))
    return out


def to_jpeg(path: Path, max_side: int, quality: int = 90, rotate: int = 0) -> bytes:
    """RGB, at most `max_side` pixels on the long edge, turned `rotate` degrees clockwise.

    EXIF orientation is applied first; `rotate` fixes paper that was photographed sideways.
    """
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if rotate % 360:
            im = im.rotate(-rotate, expand=True)
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
