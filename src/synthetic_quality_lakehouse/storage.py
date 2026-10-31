"""Generation-pinned reads and create-only writes; never overwrite/delete objects."""

import hashlib

from google.api_core.exceptions import NotFound, PreconditionFailed

from synthetic_quality_lakehouse.config import gcs_parts


class GCSObjects:
    def __init__(self, client):
        self.client = client

    def read(self, uri, generation=None, max_bytes=64 * 1024 * 1024):
        bucket, name = gcs_parts(uri)
        blob = self.client.bucket(bucket).get_blob(name, generation=generation, retry=None)
        if blob is None:
            raise FileNotFoundError("Required object absent")
        if blob.size is None or blob.size > max_bytes:
            raise ValueError("Object exceeds reviewed driver-memory bound")
        data = blob.download_as_bytes(if_generation_match=int(blob.generation), retry=None)
        if len(data) > max_bytes:
            raise ValueError("Object exceeds reviewed driver-memory bound")
        return data, int(blob.generation)

    def optional(self, uri):
        try:
            return self.read(uri)
        except (FileNotFoundError, NotFound):
            return None

    def create(self, uri, data):
        bucket, name = gcs_parts(uri)
        blob = self.client.bucket(bucket).blob(name)
        try:
            blob.upload_from_string(
                data, content_type="application/x-ndjson", if_generation_match=0, retry=None
            )
        except PreconditionFailed:
            pass
        current, generation = self.read(uri)
        if current != data:
            raise ValueError("Immutable run/object conflict; preserve it and choose a new run ID")
        return {"uri": uri, "generation": generation, "sha256": hashlib.sha256(data).hexdigest()}

    def verify(self, record):
        current, generation = self.read(record["uri"])
        if (
            generation != record["generation"]
            or hashlib.sha256(current).hexdigest() != record["sha256"]
        ):
            raise ValueError("Completed artifact changed; stop without repair")
        return current
