"""
媒体附件处理：图片校验、HEIC 转码、压缩、R2/S3 存取。
"""
from __future__ import annotations

import io
import os
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from bot.logger import get_logger
import config

logger = get_logger(__name__)

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception as e:  # pragma: no cover - import failure is surfaced at runtime
    Image = None
    ImageOps = None
    UnidentifiedImageError = Exception
    logger.warning(f"⚠️ 图片处理依赖不可用: {e}")


SUPPORTED_MIME = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
MAX_PIXELS = 25_000_000
DISPLAY_MAX_EDGE = 1600
THUMB_MAX_EDGE = 480
PENDING_MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pending_media")


class MediaError(Exception):
    """用户可见的媒体处理错误。"""


@dataclass
class StoredImage:
    bucket: str
    object_key: str
    thumbnail_key: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    original_filename: str | None


@dataclass
class CachedImage:
    content_hash: str
    path: str
    size_bytes: int


def _media_configured() -> bool:
    return bool(
        config.MEDIA_BUCKET
        and config.MEDIA_ENDPOINT
        and config.MEDIA_ACCESS_KEY_ID
        and config.MEDIA_SECRET_ACCESS_KEY
    )


def ensure_media_configured() -> None:
    if not _media_configured():
        raise MediaError(
            "media storage is not configured: set media.bucket, media.endpoint, "
            "MEDIA_ACCESS_KEY_ID and MEDIA_SECRET_ACCESS_KEY"
        )


def _s3_client():
    ensure_media_configured()
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=config.MEDIA_ENDPOINT,
        aws_access_key_id=config.MEDIA_ACCESS_KEY_ID,
        aws_secret_access_key=config.MEDIA_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _sniff_mime(data: bytes, declared_mime: str | None) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        brand = data[8:12].lower()
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    if declared_mime in SUPPORTED_MIME:
        return declared_mime
    return "application/octet-stream"


def _open_image(data: bytes):
    if Image is None:
        raise MediaError("image processing dependencies are not installed")
    try:
        img = Image.open(io.BytesIO(data))
        if (img.width or 0) * (img.height or 0) > MAX_PIXELS:
            raise MediaError(f"image is too large; max pixels is {MAX_PIXELS}")
        img.load()
    except UnidentifiedImageError as e:
        raise MediaError("unsupported or invalid image file") from e
    return ImageOps.exif_transpose(img)


def _resize(image, max_edge: int):
    img = image.copy()
    img.thumbnail((max_edge, max_edge))
    return img


def _encode(image, *, variant: Literal["display", "thumb"], prefer_webp: bool) -> tuple[bytes, str, str]:
    if prefer_webp:
        out = io.BytesIO()
        image.convert("RGB").save(
            out,
            format="WEBP",
            quality=82 if variant == "display" else 76,
            method=6,
        )
        return out.getvalue(), "image/webp", "webp"

    out = io.BytesIO()
    image.convert("RGB").save(
        out,
        format="JPEG",
        quality=82 if variant == "display" else 75,
        optimize=True,
        progressive=True,
    )
    return out.getvalue(), "image/jpeg", "jpg"


def _object_prefix(event_id: int) -> str:
    day = datetime.utcnow().strftime("%Y/%m/%d")
    return f"events/{day}/{event_id}/{uuid.uuid4().hex}"


def process_image(data: bytes, *, filename: str | None, content_type: str | None) -> tuple[bytes, bytes, str, str, int, int, str]:
    max_bytes = max(1, config.MEDIA_MAX_UPLOAD_MB) * 1024 * 1024
    if len(data) > max_bytes:
        raise MediaError(f"image is too large; max upload is {config.MEDIA_MAX_UPLOAD_MB}MB")

    sniffed = _sniff_mime(data, content_type)
    if sniffed not in SUPPORTED_MIME:
        raise MediaError(f"unsupported image type: {content_type or sniffed}")

    img = _open_image(data)
    display_img = _resize(img, DISPLAY_MAX_EDGE)
    thumb_img = _resize(img, THUMB_MAX_EDGE)

    # Screenshots and HEIC both benefit from WebP for browser display. JPEG is
    # still the safest fallback if WebP encoding ever fails.
    prefer_webp = True
    try:
        display_bytes, display_mime, display_ext = _encode(
            display_img, variant="display", prefer_webp=prefer_webp
        )
        thumb_bytes, _thumb_mime, _thumb_ext = _encode(
            thumb_img, variant="thumb", prefer_webp=prefer_webp
        )
    except Exception:
        display_bytes, display_mime, display_ext = _encode(
            display_img, variant="display", prefer_webp=False
        )
        thumb_bytes, _thumb_mime, _thumb_ext = _encode(
            thumb_img, variant="thumb", prefer_webp=False
        )
    return (
        display_bytes,
        thumb_bytes,
        display_mime,
        display_ext,
        display_img.width,
        display_img.height,
        sniffed,
    )


def cache_pending_image(data: bytes) -> CachedImage:
    os.makedirs(PENDING_MEDIA_DIR, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    path = os.path.join(PENDING_MEDIA_DIR, digest)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return CachedImage(content_hash=digest, path=path, size_bytes=len(data))


def load_pending_image(content_hash: str) -> bytes:
    digest = (content_hash or "").strip().lower()
    if not digest or any(c not in "0123456789abcdef" for c in digest):
        raise MediaError("invalid image hash")
    path = os.path.join(PENDING_MEDIA_DIR, digest)
    if not os.path.exists(path):
        raise MediaError(f"cached image not found: {digest[:12]}")
    with open(path, "rb") as f:
        return f.read()


def store_image(
    event_id: int,
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> StoredImage:
    display_bytes, thumb_bytes, display_mime, ext, width, height, _original_mime = process_image(
        data,
        filename=filename,
        content_type=content_type,
    )
    client = _s3_client()
    prefix = _object_prefix(event_id)
    object_key = f"{prefix}/display.{ext}"
    thumbnail_key = f"{prefix}/thumb.{ext}"

    for key, body in ((object_key, display_bytes), (thumbnail_key, thumb_bytes)):
        client.put_object(
            Bucket=config.MEDIA_BUCKET,
            Key=key,
            Body=body,
            ContentType=display_mime,
            CacheControl="private, max-age=31536000",
        )

    return StoredImage(
        bucket=config.MEDIA_BUCKET,
        object_key=object_key,
        thumbnail_key=thumbnail_key,
        mime_type=display_mime,
        width=width,
        height=height,
        size_bytes=len(display_bytes),
        original_filename=os.path.basename(filename) if filename else None,
    )


def presign_url(object_key: str, *, expires_in: int = 3600) -> str:
    if config.MEDIA_PUBLIC_BASE_URL:
        return f"{config.MEDIA_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
    client = _s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.MEDIA_BUCKET, "Key": object_key},
        ExpiresIn=expires_in,
    )


def attachment_view(row: dict) -> dict:
    thumb_key = row.get("thumbnail_key") or row.get("object_key")
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "kind": row.get("kind", "image"),
        "mime_type": row.get("mime_type"),
        "width": row.get("width"),
        "height": row.get("height"),
        "size_bytes": row.get("size_bytes"),
        "original_filename": row.get("original_filename"),
        "source": row.get("source"),
        "created_at": row.get("created_at"),
        "url": presign_url(row["object_key"]),
        "thumbnail_url": presign_url(thumb_key),
    }
