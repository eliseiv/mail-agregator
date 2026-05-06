"""MinIO/S3 wrapper used by api and worker.

API contract: ``docs/05-modules.md`` sec. 4.

Auth: ``S3_ACCESS_KEY`` / ``S3_SECRET_KEY`` are the **non-root** service
account created by the ``minio-bootstrap`` init container (see
``docs/06-security.md`` sec. 12, ``docs/07-deployment.md`` sec. 12). The
bucket policy gives this account only Get/Put/Delete/List on the single
bucket ``S3_BUCKET_NAME``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

import aioboto3
from botocore.exceptions import ClientError

from shared.config import Settings, get_settings
from shared.logging import get_logger

log = get_logger(__name__)

# Sanitiser for filenames embedded in S3 keys (ADR-0007).
_UNSAFE_FN_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_FN_LEN = 100
_DELETE_BATCH = 1000


def _sanitise_filename(filename: str) -> str:
    """Replace unsafe characters with underscore; clamp length to 100."""
    if not filename:
        return "file"
    safe = _UNSAFE_FN_RE.sub("_", filename)
    safe = safe.lstrip(".") or "file"  # avoid leading-dot hidden files
    return safe[:_MAX_FN_LEN]


class Storage:
    """Async MinIO/S3 client with the operations api+worker need.

    Each public coroutine opens a fresh client for the call. ``aioboto3`` is
    designed around per-call context managers; sharing a long-lived client
    across asyncio tasks is fragile.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session = aioboto3.Session()

    # ----- key building ----------------------------------------------------

    @staticmethod
    def build_key(
        user_id: int,
        mail_account_id: int,
        message_uid: int,
        attachment_id: int,
        filename: str,
    ) -> str:
        """Deterministic S3 key per ADR-0007.

        Layout::

            {user_id}/{mail_account_id}/{message_uid}/{attachment_id}_{safe_filename}
        """
        if user_id <= 0 or mail_account_id <= 0 or attachment_id <= 0:
            raise ValueError("ids must be positive integers")
        safe = _sanitise_filename(filename)
        return f"{user_id}/{mail_account_id}/{message_uid}/{attachment_id}_{safe}"

    # ----- bucket lifecycle ------------------------------------------------

    async def ensure_bucket(self) -> None:
        """Idempotent ``head_bucket`` -> ``create_bucket`` if missing.

        The init container ``minio-bootstrap`` creates the bucket; this is a
        defensive fallback so the API can boot in dev even without bootstrap.
        """
        s = self._settings
        async with self._session.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=s.S3_ACCESS_KEY,
            aws_secret_access_key=s.S3_SECRET_KEY,
            region_name=s.S3_REGION,
        ) as client:
            try:
                await client.head_bucket(Bucket=s.S3_BUCKET_NAME)
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
            # Create on the same client.
            await client.create_bucket(Bucket=s.S3_BUCKET_NAME)
            log.info("storage_bucket_created", bucket=s.S3_BUCKET_NAME)

    # ----- objects ---------------------------------------------------------

    async def put_object(
        self,
        key: str,
        data: bytes | AsyncIterable[bytes],
        content_type: str | None,
    ) -> None:
        """Upload an object. ``data`` is either a ``bytes`` blob or an
        ``AsyncIterable[bytes]`` (for streaming uploads).

        Streaming path: we materialise the iterator into a single ``bytes``
        before handing it to ``put_object`` because ``aioboto3``'s
        ``put_object`` ``Body`` parameter does not natively accept
        ``AsyncIterable``. This keeps the public surface consistent with
        ``docs/05-modules.md`` sec. 4 (which advertises both shapes); when
        we need true streaming for very large files we'll switch to the
        multipart upload API. For attachments capped at 25 MiB
        (``MAX_ATTACHMENT_BYTES``) the buffered path is fine.
        """
        if not isinstance(data, bytes):
            chunks: list[bytes] = []
            async for chunk in data:
                chunks.append(chunk)
            data = b"".join(chunks)

        s = self._settings
        kwargs: dict[str, object] = {
            "Bucket": s.S3_BUCKET_NAME,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        async with self._session.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=s.S3_ACCESS_KEY,
            aws_secret_access_key=s.S3_SECRET_KEY,
            region_name=s.S3_REGION,
        ) as client:
            await client.put_object(**kwargs)

    async def get_object_stream(
        self, key: str, *, chunk_size: int = 64 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream the object body in chunks. Caller is responsible for
        consuming the iterator promptly — the underlying client is held open
        for the lifetime of the iteration.
        """
        s = self._settings
        async with self._session.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=s.S3_ACCESS_KEY,
            aws_secret_access_key=s.S3_SECRET_KEY,
            region_name=s.S3_REGION,
        ) as client:
            obj = await client.get_object(Bucket=s.S3_BUCKET_NAME, Key=key)
            body = obj["Body"]
            try:
                while True:
                    chunk = await body.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

    async def get_object_metadata(self, key: str) -> dict[str, str]:
        """Return ``ContentType`` / ``ContentLength`` for a key.

        Used by the download endpoint to set headers without re-streaming.
        """
        s = self._settings
        async with self._session.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=s.S3_ACCESS_KEY,
            aws_secret_access_key=s.S3_SECRET_KEY,
            region_name=s.S3_REGION,
        ) as client:
            head = await client.head_object(Bucket=s.S3_BUCKET_NAME, Key=key)
            return {
                "ContentType": head.get("ContentType", "application/octet-stream"),
                "ContentLength": str(head.get("ContentLength", 0)),
            }

    async def delete_objects(self, keys: list[str]) -> None:
        """Delete objects in batches of 1000 (S3 limit).

        Partial failures are logged at WARNING; we do not retry. Orphan
        objects are acceptable per ADR-0007 (orphan_scan is TD-004).
        """
        if not keys:
            return
        s = self._settings
        async with self._session.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=s.S3_ACCESS_KEY,
            aws_secret_access_key=s.S3_SECRET_KEY,
            region_name=s.S3_REGION,
        ) as client:
            for i in range(0, len(keys), _DELETE_BATCH):
                batch = keys[i : i + _DELETE_BATCH]
                response = await client.delete_objects(
                    Bucket=s.S3_BUCKET_NAME,
                    Delete={
                        "Objects": [{"Key": k} for k in batch],
                        "Quiet": True,
                    },
                )
                errors = response.get("Errors", [])
                if errors:
                    log.warning(
                        "storage_delete_partial_failure",
                        error_count=len(errors),
                        sample_keys=[e.get("Key") for e in errors[:5]],
                    )

    async def delete_prefix(self, prefix: str) -> int:
        """List + batch-delete every object under ``prefix``. Returns count."""
        if not prefix:
            raise ValueError("prefix must not be empty")
        s = self._settings
        deleted = 0
        async with self._session.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL,
            aws_access_key_id=s.S3_ACCESS_KEY,
            aws_secret_access_key=s.S3_SECRET_KEY,
            region_name=s.S3_REGION,
        ) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=s.S3_BUCKET_NAME, Prefix=prefix):
                contents = page.get("Contents", [])
                if not contents:
                    continue
                keys = [item["Key"] for item in contents]
                # Delete in batches of 1000 within the same client.
                for i in range(0, len(keys), _DELETE_BATCH):
                    batch = keys[i : i + _DELETE_BATCH]
                    await client.delete_objects(
                        Bucket=s.S3_BUCKET_NAME,
                        Delete={
                            "Objects": [{"Key": k} for k in batch],
                            "Quiet": True,
                        },
                    )
                    deleted += len(batch)
        return deleted

    async def health_check(self) -> bool:
        """Used by ``/readyz``: returns True iff the configured bucket exists."""
        s = self._settings
        try:
            async with self._session.client(
                "s3",
                endpoint_url=s.S3_ENDPOINT_URL,
                aws_access_key_id=s.S3_ACCESS_KEY,
                aws_secret_access_key=s.S3_SECRET_KEY,
                region_name=s.S3_REGION,
            ) as client:
                await client.head_bucket(Bucket=s.S3_BUCKET_NAME)
            return True
        except ClientError:
            return False


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
