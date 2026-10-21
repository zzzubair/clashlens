"""Notebook pictures: one picture per base (migration 0125).

A Clasher uploads a picture of a base; Pillow turns it into a WebP picture
at most 1,600 pixels on its longer side and a 96-pixel square small copy
for lists, both without the upload's metadata (such as where a photo was
taken). They go to a private bucket, Cloudflare R2 in production, and the
notebook links to them with signed links that expire after an hour.

Each Clash Lens account can upload 30 pictures in any 24 hours. A picture
replaced, removed, or left behind by its deleted base or account stays a day,
so Undo can put a deleted base back with it, and is then deleted, row and
files, by a later upload. Without a configured bucket the notebook works
with no pictures.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import certifi
import urllib3
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from minio import Minio
from PIL import Image, ImageOps
from pydantic import Field

from .api import ApiError, StrictBody
from .api_db import ApiDatabase, _text
from .domain import ranked_day_for

PICTURE_PATH = "/v1/account/notebook/picture"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# The upload comes base64-encoded in JSON: 4 characters for every 3 bytes.
MAX_BODY_BYTES = MAX_UPLOAD_BYTES * 4 // 3 + 1024
# A 50-megapixel picture takes about 200 MB to decode; larger ones are refused.
MAX_PIXELS = 50_000_000
FULL_SIDE = 1600
SMALL_SIDE = 96
DAILY_UPLOADS = 30
# Pictures the API shrinks at once: one decode can take about 400 MB of the
# API's 1 GiB. Another upload waits at most this long, well inside its
# signature's life, then is told to try again.
SHRINKING = threading.BoundedSemaphore(1)
BUSY_WAIT_SECONDS = 2
LINK_LIFETIME = timedelta(hours=1)
# Links are signed from the start of each half hour, so a page loaded again
# within it gets the same links and the browser's saved copy, and every link
# has 30 to 60 minutes left.
LINK_WINDOW = timedelta(minutes=30)
# Off its base this long, a picture is deleted.
KEEP_LOOSE = timedelta(days=1)
SWEEP_BATCH = 20
# The private Cloudflare R2 bucket and the key files the API container
# mounts; local tests name their own store with CLASHLENS_PICTURES_*.
R2_ENDPOINT = "8d7a40d7dce99455d64eaba26d180709.eu.r2.cloudflarestorage.com"
R2_BUCKET = "clashlens-notebook-pictures"
ACCESS_KEY_FILE = "/run/secrets/pictures-access-key"
SECRET_KEY_FILE = "/run/secrets/pictures-secret-key"
_HOST = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_KEY = re.compile(r"[0-9a-f]{32}")


class PictureStore:
    """The private bucket: upload, delete and link to stored pictures."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        secure: bool = True,
    ) -> None:
        if not _HOST.fullmatch(endpoint):
            raise ValueError("pictures endpoint must be a host and port without a scheme or path")
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("pictures bucket is invalid")
        self.bucket = bucket
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            # Known up front, so signing a link never asks the bucket.
            region=region,
            http_client=urllib3.PoolManager(
                maxsize=4,
                timeout=urllib3.Timeout(connect=5, read=20),
                retries=urllib3.Retry(
                    total=2, backoff_factor=0.2, status_forcelist=(500, 502, 503, 504)
                ),
                cert_reqs="CERT_REQUIRED" if secure else "CERT_NONE",
                ca_certs=certifi.where(),
            ),
        )

    def put(self, name: str, body: bytes) -> None:
        self.client.put_object(
            self.bucket, name, io.BytesIO(body), len(body), content_type="image/webp",
            # A name is never reused, so the browser may keep its copy.
            metadata={"Cache-Control": "private, max-age=3600, immutable"},
        )

    def remove(self, name: str) -> None:
        self.client.remove_object(self.bucket, name)

    def link(self, name: str, now: datetime) -> str:
        signed = datetime.fromtimestamp(
            now.timestamp() // LINK_WINDOW.total_seconds() * LINK_WINDOW.total_seconds(),
            tz=now.tzinfo,
        )
        return self.client.presigned_get_object(
            self.bucket, name, expires=LINK_LIFETIME, request_date=signed
        )


def store_from_environment(env: Mapping[str, str] = os.environ) -> PictureStore | None:
    """The R2 bucket, or the store CLASHLENS_PICTURES_* names; None without
    both key files, or while they hold the placeholder `disabled`. A
    plain-http store is for local tests only."""
    secure = env.get("CLASHLENS_PICTURES_SECURE", "true") != "false"
    if not secure and env.get("CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN") != "true":
        raise ValueError("an insecure pictures endpoint is for local tests only")

    def key(name: str, default: str) -> str | None:
        path = Path(env.get(name, default))
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError(f"{name} must name a file holding one key")
        return None if value == "disabled" else value

    access_key = key("CLASHLENS_PICTURES_ACCESS_KEY_FILE", ACCESS_KEY_FILE)
    secret_key = key("CLASHLENS_PICTURES_SECRET_KEY_FILE", SECRET_KEY_FILE)
    if access_key is None or secret_key is None:
        return None
    return PictureStore(
        endpoint=env.get("CLASHLENS_PICTURES_ENDPOINT", R2_ENDPOINT),
        bucket=env.get("CLASHLENS_PICTURES_BUCKET", R2_BUCKET),
        region=env.get("CLASHLENS_PICTURES_REGION", "auto"),
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def shrink(raw: bytes) -> tuple[bytes, bytes]:
    """The picture and its small copy as WebP, from a JPEG, PNG or WebP
    upload, turned the way the phone held it."""
    try:
        with Image.open(io.BytesIO(raw), formats=("JPEG", "PNG", "WEBP")) as opened:
            # Only the header is read so far.
            if opened.width * opened.height > MAX_PIXELS:
                raise ApiError(422, "picture_too_large")
            # A JPEG decodes straight at about the size needed.
            opened.draft("RGB", (FULL_SIDE, FULL_SIDE))
            image = ImageOps.exif_transpose(opened)
            alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if alpha else "RGB")
            image.thumbnail((FULL_SIDE, FULL_SIDE), Image.Resampling.LANCZOS)
            small = ImageOps.fit(image, (SMALL_SIDE, SMALL_SIDE), Image.Resampling.LANCZOS)
            return _webp(image, 80), _webp(small, 75)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        # Not a picture, a kind other than those three, or cut short.
        raise ApiError(422, "not_a_picture") from None


def _webp(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=quality, method=4)
    return buffer.getvalue()


def _names(key: str) -> tuple[str, str]:
    return f"{key}/full.webp", f"{key}/small.webp"


def links(store: PictureStore | None, key: str | None, now: datetime) -> dict[str, str] | None:
    """A picture's two links, or None without a picture or a bucket."""
    if store is None or key is None:
        return None
    full, small = _names(key)
    return {"full": store.link(full, now), "small": store.link(small, now)}


def keys(connection: Any, base_ids: list[int]) -> dict[int, str]:
    """The picture on each of these bases that has one."""
    return {
        int(base_id): _text(key)
        for base_id, key in connection.execute(
            "SELECT base_id, key FROM notebook_pictures WHERE base_id = ANY(%s)",
            (base_ids,),
        ).fetchall()
    }


def detach(connection: Any, base_id: int) -> str | None:
    """Take the base's picture off it, from now; its key, if it had one."""
    row = connection.execute(
        "UPDATE notebook_pictures SET base_id = NULL, detached_at = clock_timestamp()"
        " WHERE base_id = %s RETURNING key",
        (base_id,),
    ).fetchone()
    return None if row is None else _text(row[0])


def reattach(connection: Any, account_id: int, key: str, base_id: int) -> None:
    """Put back this account's picture ``key`` on a base Undo restored, if it
    has not been deleted meanwhile."""
    if _KEY.fullmatch(key):
        connection.execute(
            "UPDATE notebook_pictures SET base_id = %s, detached_at = NULL"
            " WHERE key = %s AND account_id = %s AND base_id IS NULL",
            (base_id, key, account_id),
        )


def _lock_base(connection: Any, account_id: int, code: str, now: datetime) -> int:
    """This Season's base ``code`` in the account's notebook, with the
    account held so its uploads count one at a time."""
    connection.execute(
        "SELECT 1 FROM clash_lens_accounts WHERE id = %s FOR NO KEY UPDATE", (account_id,)
    )
    row = connection.execute(
        "SELECT id FROM notebook_bases WHERE code = %s AND account_id = %s AND season_id = %s",
        (code, account_id, ranked_day_for(now).official_season_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "base_not_found")
    return int(row[0])


def _within_limit(connection: Any, account_id: int, code: str, now: datetime) -> int:
    """The base, as `_lock_base`, while the account has uploads left today."""
    base_id = _lock_base(connection, account_id, code, now)
    (uploaded,) = connection.execute(
        "SELECT count(*) FROM notebook_pictures WHERE account_id = %s"
        " AND uploaded_at > clock_timestamp() - interval '1 day'",
        (account_id,),
    ).fetchone()
    if uploaded >= DAILY_UPLOADS:
        raise ApiError(429, "daily_limit")
    return base_id


def upload(
    database: ApiDatabase,
    store: PictureStore | None,
    account_id: int,
    code: str,
    raw: bytes,
    *,
    now: datetime,
) -> str:
    """Store ``raw`` as the base's picture, replacing any it had; its key."""
    if store is None:
        raise ApiError(503, "pictures_unavailable")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "picture_too_large")
    # Refused before the costly decode, and checked again once it is done.
    with database.pool.connection() as connection, connection.transaction():
        _within_limit(connection, account_id, code, now)
    if not SHRINKING.acquire(timeout=BUSY_WAIT_SECONDS):
        raise ApiError(503, "pictures_busy")
    try:
        full, small = shrink(raw)
    finally:
        SHRINKING.release()
    key = secrets.token_hex(16)
    with database.pool.connection() as connection, connection.transaction():
        _within_limit(connection, account_id, code, now)
        # Counted, and deleted in a day if the upload stops here.
        connection.execute(
            "INSERT INTO notebook_pictures (account_id, key, bytes) VALUES (%s, %s, %s)",
            (account_id, key, len(full) + len(small)),
        )
    try:
        for name, body in zip(_names(key), (full, small), strict=True):
            store.put(name, body)
    except Exception:  # noqa: BLE001 - the bucket's errors may name it.
        raise ApiError(503, "pictures_unavailable") from None
    with database.pool.connection() as connection, connection.transaction():
        base_id = _lock_base(connection, account_id, code, now)
        detach(connection, base_id)
        connection.execute(
            "UPDATE notebook_pictures SET base_id = %s WHERE key = %s", (base_id, key)
        )
    sweep(database, store)
    return key


def remove(database: ApiDatabase, account_id: int, code: str, *, now: datetime) -> None:
    with database.pool.connection() as connection, connection.transaction():
        detach(connection, _lock_base(connection, account_id, code, now))


def sweep(database: ApiDatabase, store: PictureStore, limit: int = SWEEP_BATCH) -> int:
    """Delete up to ``limit`` pictures that have been off their base for a
    day, oldest first, files then row; how many. Stops quietly when the
    bucket fails, for the next upload to carry on."""
    with database.pool.connection() as connection:
        with connection.transaction():
            loose = connection.execute(
                "SELECT id, key FROM notebook_pictures WHERE base_id IS NULL"
                " AND COALESCE(detached_at, uploaded_at) < clock_timestamp() - %s"
                " ORDER BY COALESCE(detached_at, uploaded_at) LIMIT %s",
                (KEEP_LOOSE, limit),
            ).fetchall()
        deleted = 0
        for picture_id, key in loose:
            try:
                for name in _names(_text(key)):
                    store.remove(name)
            except Exception:  # noqa: BLE001 - kept for the next upload.
                break
            with connection.transaction():
                connection.execute(
                    "DELETE FROM notebook_pictures WHERE id = %s AND base_id IS NULL",
                    (picture_id,),
                )
            deleted += 1
    return deleted


class PictureChange(StrictBody):
    base: str = Field(max_length=12)
    # The upload, base64-encoded; None removes the base's picture.
    image: str | None = Field(max_length=MAX_BODY_BYTES)


def register_picture_routes(
    router: APIRouter,
    *,
    database: ApiDatabase,
    current_time: Callable[[], datetime],
    store: PictureStore | None,
    account: Callable[[Request, str], int],
) -> None:
    """Upload or remove one base's picture, answering its new links."""

    @router.post(PICTURE_PATH)
    def change(body: PictureChange, request: Request) -> JSONResponse:
        from .api_notebook import parse_code

        account_id = account(request, "account.update")
        code = parse_code(body.base)
        now = current_time()
        if body.image is None:
            remove(database, account_id, code, now=now)
            key = None
        else:
            try:
                raw = base64.b64decode(body.image, validate=True)
            except (binascii.Error, ValueError):
                raise ApiError(422, "not_a_picture") from None
            key = upload(database, store, account_id, code, raw, now=now)
        return JSONResponse(content={
            "kind": "notebook-picture", "base": code, "picture": links(store, key, now),
        })
