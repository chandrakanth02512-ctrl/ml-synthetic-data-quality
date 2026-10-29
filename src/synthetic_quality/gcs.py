"""Create-only GCS storage with bounded retries and generation-pinned verification."""

import hashlib
from dataclasses import dataclass

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY

from synthetic_quality.cloud_config import validate_object_name

# Retrying writes is safe only because every upload supplies if_generation_match=0.
RETRY = DEFAULT_RETRY.with_timeout(60)
TIMEOUT = 30


class RunConflictError(RuntimeError):
    """An immutable run/object differs from the requested bytes or manifest."""


@dataclass(frozen=True)
class StoredObject:
    name: str
    generation: int
    data: bytes

    def record(self) -> dict:
        return {
            "name": self.name,
            "generation": self.generation,
            "size_bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


class GCSStore:
    def __init__(self, client: storage.Client, bucket: str):
        self.bucket = client.bucket(bucket)

    def read(self, name: str) -> StoredObject | None:
        blob = self.bucket.get_blob(validate_object_name(name), timeout=TIMEOUT, retry=RETRY)
        if blob is None:
            return None
        generation = int(blob.generation)
        data = blob.download_as_bytes(
            if_generation_match=generation, timeout=TIMEOUT, retry=RETRY, checksum="auto"
        )
        return StoredObject(name, generation, data)

    def create(self, name: str, data: bytes, content_type: str) -> StoredObject:
        blob = self.bucket.blob(validate_object_name(name))
        try:
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=0,
                timeout=TIMEOUT,
                retry=RETRY,
                # SDK checksum failure cleanup can delete objects. Instead verify the
                # downloaded bytes below; this adapter never requests object deletion.
                checksum=None,
            )
        except PreconditionFailed:
            # Covers concurrent creates and retry after a committed upload lost its ACK.
            pass
        existing = self.read(name)
        if existing is None or existing.data != data:
            raise RunConflictError(f"Immutable object conflict: {name}; use a new run_id")
        return existing
