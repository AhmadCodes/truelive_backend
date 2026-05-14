"""
MinIO / S3-compatible blob storage client.

Lazy-initialized so importing this module is safe without credentials configured.
The actual `minio` SDK is required at runtime; ImportError is surfaced when callers
try to use the client without the dependency installed.

Storage URIs are stored in DB as `s3://<bucket>/<key>` for forward-compat with a
future move off MinIO. Presigned URLs are minted via the configured endpoint
(`s3.usvg.ai`) so they can be served externally to the downstream platform.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class MinioClientError(RuntimeError):
    pass


class MinioStorage:
    """Thin wrapper around minio.Minio for raw mail + alert media buckets."""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from minio import Minio  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise MinioClientError(
                    "minio SDK not installed. Add `minio>=7.2` to requirements.txt"
                ) from exc

            access = settings.MINIO_ACCESS_KEY
            secret = settings.MINIO_SECRET_KEY
            if not access or not secret:
                raise MinioClientError(
                    "MINIO_ACCESS_KEY / MINIO_SECRET_KEY not configured in env"
                )
            self._client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=access,
                secret_key=secret,
                secure=settings.MINIO_SECURE,
                region=settings.MINIO_REGION,
            )
        return self._client

    # --- raw mail ----------------------------------------------------- #

    def put_raw_mail(self, msg_id: str, content: bytes, received_at: Optional[datetime] = None) -> str:
        """Stream raw .eml bytes to MinIO. Returns the s3:// URI for the row."""
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        key = self._raw_mail_key(msg_id, received_at)
        self._put(settings.MINIO_RAW_MAIL_BUCKET, key, content, "message/rfc822")
        return f"s3://{settings.MINIO_RAW_MAIL_BUCKET}/{key}"

    @staticmethod
    def _raw_mail_key(msg_id: str, received_at: datetime) -> str:
        return f"{received_at:%Y/%m/%d}/{msg_id}.eml"

    # --- media -------------------------------------------------------- #

    def put_alert_media(
        self, alert_id: str, media_id: str, content: bytes,
        content_type: Optional[str] = None, extension: str = "bin",
        created_at: Optional[datetime] = None,
    ) -> str:
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        ext = extension.lstrip(".") or "bin"
        key = f"{created_at:%Y/%m/%d}/{alert_id}/{media_id}.{ext}"
        self._put(
            settings.MINIO_ALERT_MEDIA_BUCKET, key, content,
            content_type or "application/octet-stream",
        )
        return f"s3://{settings.MINIO_ALERT_MEDIA_BUCKET}/{key}"

    # --- presign / fetch ---------------------------------------------- #

    def presign_get(
        self, storage_uri: str, expires_days: Optional[int] = None,
    ) -> tuple[str, datetime]:
        """Mint a presigned GET URL. Returns (url, expires_at_utc)."""
        bucket, key = self._parse_s3_uri(storage_uri)
        days = expires_days or settings.MINIO_PRESIGN_EXPIRY_DAYS
        expiry = timedelta(days=days)
        url = self.client.presigned_get_object(bucket, key, expires=expiry)
        return url, datetime.now(timezone.utc) + expiry

    def fetch(self, storage_uri: str) -> bytes:
        bucket, key = self._parse_s3_uri(storage_uri)
        response = self.client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    # --- internal ----------------------------------------------------- #

    def _put(self, bucket: str, key: str, content: bytes, content_type: str) -> None:
        from minio.error import S3Error  # type: ignore
        stream: BinaryIO = io.BytesIO(content)
        try:
            self.client.put_object(
                bucket, key, stream, length=len(content),
                content_type=content_type,
            )
        except S3Error as exc:
            logger.exception(
                "MinIO put failed", extra={"bucket": bucket, "key": key},
            )
            raise MinioClientError(f"MinIO put failed: {exc}") from exc

    @staticmethod
    def _parse_s3_uri(uri: str) -> tuple[str, str]:
        if not uri.startswith("s3://"):
            raise ValueError(f"expected s3:// URI, got: {uri!r}")
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise ValueError(f"malformed s3 URI: {uri!r}")
        return bucket, key


# Module-level singleton — created lazily on first attribute access.
storage = MinioStorage()
