"""Validate and prepare an uploaded photo or video for WhatsApp.

Never trust the file name or the browser's content type: the file's own first bytes decide what it is. Photos are
re-encoded (rotated upright, shrunk, EXIF/GPS stripped) so they send fast on mobile data and carry no hidden payload;
videos are only validated (WhatsApp wants MP4 under 16 MB) — we do not transcode.

Every MediaError message is written for the person uploading, so the API can show it as is.
"""
from __future__ import annotations
import io
from dataclasses import dataclass

MB = 1024 * 1024
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}


class MediaError(ValueError):
    """Safe to show to the user."""


@dataclass
class Processed:
    kind: str           # image | video
    ext: str            # jpg | mp4
    content_type: str
    data: bytes
    original_bytes: int


def sniff(data: bytes) -> str | None:
    """jpeg | png | webp | gif | mp4 | heic | mov | None — from the file's own bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"GIF8":
        return "gif"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in HEIC_BRANDS:
            return "heic"
        if brand == b"qt  ":
            return "mov"
        return "mp4"
    return None


def _prepare_image(data: bytes, max_px: int, quality: int) -> bytes:
    try:
        from PIL import Image, ImageOps
    except ImportError as e:      # pragma: no cover - dependency is in requirements.txt
        raise MediaError("Image processing isn't installed on the server (pip install Pillow).") from e
    Image.MAX_IMAGE_PIXELS = 60_000_000          # refuse "decompression bombs" (tiny file, enormous canvas)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)       # phones store rotation in metadata; apply it, then drop the metadata
    except Exception as e:
        raise MediaError("That image looks damaged or isn't a real photo. Try exporting it again as JPG or PNG.") from e
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[-1])
        img = base
    else:
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def process(data: bytes, *, max_image_mb: int = 15, max_video_mb: int = 16, max_px: int = 1600, quality: int = 85) -> Processed:
    if not data:
        raise MediaError("That file is empty.")
    kind = sniff(data)
    if kind in ("jpeg", "png", "webp", "gif"):
        if len(data) > max_image_mb * MB:
            raise MediaError(f"That photo is {len(data) / MB:.0f} MB. The limit is {max_image_mb} MB — please use a smaller one.")
        out = _prepare_image(data, max_px, quality)
        return Processed("image", "jpg", "image/jpeg", out, len(data))
    if kind == "mp4":
        if len(data) > max_video_mb * MB:
            raise MediaError(f"That video is {len(data) / MB:.0f} MB. WhatsApp's limit is {max_video_mb} MB — trim or compress it first.")
        return Processed("video", "mp4", "video/mp4", data, len(data))
    if kind == "heic":
        raise MediaError("That's an iPhone HEIC photo. Export or share it as JPG first (or set the camera to \"Most Compatible\").")
    if kind == "mov":
        raise MediaError("That's a .mov video. Please convert it to .mp4 — WhatsApp plays MP4 reliably.")
    raise MediaError("Only photos (JPG, PNG, WebP) and MP4 videos can be uploaded.")
