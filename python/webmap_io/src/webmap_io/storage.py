"""Object storage client for the data plane.

GeoParquet features, COG grids, renders, and uploads all live here
(`01-architecture.md` §1). Object storage is **not reachable by users**: it
sits on the internal network with credentials only the API and worker hold,
and every read goes through a permission check in a service function before
the object is opened (`02-data-model.md` §4.1).

That makes this module a boundary worth keeping narrow. It moves bytes to and
from keys; it makes no decision about who may do so.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = Any


@dataclass(frozen=True)
class StorageConfig:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"


def client(config: StorageConfig) -> S3Client:
    """An S3 client for a MinIO-compatible endpoint.

    `path` addressing rather than virtual-host: MinIO and every other
    S3-compatible store that is not AWS needs it, and getting this wrong
    surfaces as a DNS failure rather than an S3 error.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )


def ensure_bucket(s3: S3Client, bucket: str) -> None:
    from botocore.exceptions import ClientError

    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        s3.create_bucket(Bucket=bucket)


def put_file(s3: S3Client, bucket: str, key: str, path: Path) -> None:
    with path.open("rb") as handle:
        s3.put_object(Bucket=bucket, Key=key, Body=handle)


def put_bytes(
    s3: S3Client, bucket: str, key: str, data: bytes, *, content_type: str | None = None
) -> None:
    """Write bytes directly.

    For objects that were never files — a render's PNG comes out of a browser
    as bytes, and staging it through a temp file only to read it back would be
    two extra copies of a four-megabyte image.
    """
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        **({"ContentType": content_type} if content_type else {}),
    )


def get_bytes(s3: S3Client, bucket: str, key: str) -> bytes:
    response = s3.get_object(Bucket=bucket, Key=key)
    return bytes(response["Body"].read())


def get_file(s3: S3Client, bucket: str, key: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    response = s3.get_object(Bucket=bucket, Key=key)
    with dest.open("wb") as handle:
        handle.write(response["Body"].read())
    return dest


def iter_keys(s3: S3Client, bucket: str, prefix: str) -> Iterator[str]:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield str(item["Key"])


def feature_key(dataset_id: str, version: int) -> str:
    """`features/ds_<uuid_hex>/v<N>.parquet` — `02-data-model.md` §3.5.1.

    Versioned rather than overwritten. Objects are immutable, so two
    concurrent editors produce two separately-named objects and contend only
    on the version pointer (`adr/0005-single-editor-persistence.md`).
    """
    return f"features/ds_{dataset_id.replace('-', '')}/v{version}.parquet"


def grid_key(dataset_id: str) -> str:
    return f"grids/ds_{dataset_id.replace('-', '')}.tif"
