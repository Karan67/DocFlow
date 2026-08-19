"""Phase 6: the S3 storage backend.

Run inside the worker container:  docker compose exec worker pytest

Tested against MinIO rather than a mock. A mocked S3 test would have passed
while telling us nothing about the two things that actually bite: whether the
credentials and endpoint wiring work, and whether `open()` returns something
pypdf can seek in. S3's streaming body is forward-only, and a PDF's
cross-reference table lives at the end of the file.

The parametrised tests below run the *same* assertions against both backends.
That parity is the whole claim the Phase 1 abstraction was making.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest

from core.config import settings
from core.storage import (
    FileTooLarge,
    LocalDiskStorage,
    S3Storage,
    StorageError,
    build_storage,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def s3_storage():
    """A real S3 client pointed at MinIO, in a per-test key prefix."""
    # Fail loudly rather than falling back to a hostname that only resolves
    # inside the Docker network, and rather than skipping - a silently skipped
    # storage test is worse than no test.
    assert settings.S3_ENDPOINT_URL, (
        "S3_ENDPOINT_URL must point at an S3-compatible endpoint to run these "
        "tests. Locally: docker compose up -d minio minio-init"
    )
    return S3Storage(
        bucket=settings.S3_BUCKET,
        max_bytes=settings.MAX_UPLOAD_BYTES,
        prefix=f"test-{uuid.uuid4().hex}/",
        endpoint_url=settings.S3_ENDPOINT_URL,
        region=settings.AWS_REGION,
    )


@pytest.fixture
def local_storage(tmp_path):
    return LocalDiskStorage(root=tmp_path, max_bytes=settings.MAX_UPLOAD_BYTES)


@pytest.fixture(params=["local", "s3"])
def storage(request, local_storage, s3_storage):
    """Both backends, so the contract is asserted identically against each."""
    return local_storage if request.param == "local" else s3_storage


# --------------------------------------------------------------------------
# contract, asserted against both backends
# --------------------------------------------------------------------------


def test_round_trip_preserves_bytes_exactly(storage):
    payload = b"%PDF-1.4 round trip \x00\x01\x02 binary safe\n" * 100

    stored = storage.save(io.BytesIO(payload), "doc.pdf")
    with storage.open(stored.key) as handle:
        assert handle.read() == payload


def test_reports_size_and_content_hash(storage):
    payload = b"hash me" * 1000

    stored = storage.save(io.BytesIO(payload), "doc.pdf")

    assert stored.size == len(payload)
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()


def test_key_keeps_the_extension_but_not_the_name(storage):
    stored = storage.save(io.BytesIO(b"data"), "Some Customer Invoice.PDF")

    assert stored.key.endswith(".pdf")
    assert "Invoice" not in stored.key, "the key must not leak the original name"


def test_open_is_seekable(storage):
    """pypdf reads a PDF's xref table from the end, so it must be able to seek."""
    payload = b"0123456789" * 50

    stored = storage.save(io.BytesIO(payload), "doc.pdf")
    with storage.open(stored.key) as handle:
        handle.seek(-10, io.SEEK_END)
        assert handle.read() == b"0123456789"
        handle.seek(0)
        assert handle.read(5) == b"01234"


def test_oversized_upload_is_rejected(storage):
    storage.max_bytes = 1024
    oversized = io.BytesIO(b"x" * 4096)

    with pytest.raises(FileTooLarge):
        storage.save(oversized, "big.pdf")


def test_missing_key_raises_storage_error(storage):
    with pytest.raises(StorageError):
        storage.open(f"{uuid.uuid4().hex}.pdf")


def test_delete_removes_the_object(storage):
    stored = storage.save(io.BytesIO(b"delete me"), "doc.pdf")

    storage.delete(stored.key)

    with pytest.raises(StorageError):
        storage.open(stored.key)


def test_delete_is_idempotent(storage):
    """Cleanup paths call delete without checking, so it must not raise."""
    stored = storage.save(io.BytesIO(b"data"), "doc.pdf")
    storage.delete(stored.key)
    storage.delete(stored.key)


def test_suspicious_keys_are_refused(storage):
    for key in ["../etc/passwd", "nested/key.pdf", "..", ""]:
        with pytest.raises(StorageError):
            storage.open(key)


# --------------------------------------------------------------------------
# backend selection
# --------------------------------------------------------------------------


def test_build_storage_honours_the_configured_backend(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    assert isinstance(build_storage(), LocalDiskStorage)

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    assert isinstance(build_storage(), S3Storage)


def test_s3_keys_are_namespaced_by_prefix(s3_storage):
    """Prefix keeps uploads out of the bucket root without leaking into the key."""
    stored = s3_storage.save(io.BytesIO(b"data"), "doc.pdf")

    assert not stored.key.startswith(s3_storage.prefix), "prefix stays internal"
    listing = s3_storage.client.list_objects_v2(
        Bucket=s3_storage.bucket, Prefix=s3_storage.prefix
    )
    assert [obj["Key"] for obj in listing["Contents"]] == [
        f"{s3_storage.prefix}{stored.key}"
    ]


def test_the_whole_pipeline_runs_on_s3(
    monkeypatch, upload, run_pipeline, read_job, fake_embedder, unique_pdf
):
    """The claim the Phase 1 abstraction made, tested end to end.

    Same upload route, same worker stages, same job row - only the backend
    behind `file_path` differs, and no migration was involved.
    """
    from core import storage as storage_module

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    storage_module.reset_storage()

    try:
        response = upload(unique_pdf("on-s3"), "on-s3.pdf")
        assert response.status_code == 202
        job_id = uuid.UUID(response.json()["job_id"])

        run_pipeline(job_id)

        job = read_job(job_id)
        assert job["status"] == "DONE"
        assert job["result"]["page_count"] == 2
        # file_path is the same opaque key either way - nothing in the schema
        # or the pipeline knows which backend produced it.
        assert "/" not in job["result"].get("source", "")
    finally:
        storage_module.reset_storage()
