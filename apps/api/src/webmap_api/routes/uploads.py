"""Upload and preview endpoints. `11-file-io.md` §2.1, §3.1.

Two endpoints in a deliberate order: preview first, then import. The preview
is what makes "the column mapping is required, not sniffed" workable — a rule
with no way to discover the right answer is just an obstacle.
"""

import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile, status

from webmap_api.dependencies import AppSettings, CurrentPrincipal, ScopedConn
from webmap_api.routes.jobs import queue, submit_job
from webmap_core.models import DatasetKind
from webmap_core.permissions import Visibility
from webmap_core.services.ingest import IngestOptions, ingest_upload, preview

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])

#: Read in chunks rather than `await file.read()`. The size cap is enforced in
#: the pipeline, but a single read would have to hold the whole upload in
#: memory first, which is how the cap gets reached by the API rather than by
#: the file.
CHUNK_BYTES = 1024 * 1024

#: Above this, the read goes to the queue. `10-jobs-async.md` §6: blocking an
#: API worker for five seconds is acceptable and thirty is not, because it
#: starves every other request on the process.
#:
#: Bytes rather than an estimate of seconds, because at upload time the file is
#: the only thing known — the feature count is on the far side of the read this
#: threshold decides whether to do. 32 MB of shapefile is a few hundred thousand
#: polygons, which is seconds of pyogrio and comfortably inside the budget.
INLINE_LIMIT_BYTES = 32 * 1024 * 1024


async def _spool(file: UploadFile, directory: Path) -> Path:
    """Stream the upload to disk under a sanitized name.

    The client-supplied filename reaches a filesystem path here, which is
    exactly the boundary `03-auth-security.md` §9 is about.
    """
    from webmap_io.read import sanitize_name

    name = sanitize_name(file.filename or "upload", fallback="upload")
    target = directory / name
    with target.open("wb") as sink:
        while chunk := await file.read(CHUNK_BYTES):
            sink.write(chunk)
    return target


@router.post("/preview")
async def preview_upload(
    principal: CurrentPrincipal,
    file: Annotated[UploadFile, File(description="The file, or a zip of one")],
    encoding: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Inspect a file without registering it.

    Returns the first rows, the columns, and for tabular data a *proposed*
    mapping for the caller to confirm. A proposal the user confirms is a
    different thing from a guess the software acts on.

    Requires authentication but no permission check: nothing is read from the
    registry and nothing is written. The file is the caller's own bytes.
    """
    with tempfile.TemporaryDirectory(prefix="webmap-preview-") as workdir:
        source = await _spool(file, Path(workdir))
        return preview(source, encoding=encoding)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_dataset(
    request: Request,
    principal: CurrentPrincipal,
    conn: ScopedConn,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()],
    project_id: Annotated[UUID | None, Form()] = None,
    visibility: Annotated[Visibility, Form()] = Visibility.TEAM,
    owner_team_id: Annotated[UUID | None, Form()] = None,
    kind: Annotated[DatasetKind | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    srid: Annotated[int | None, Form(description="EPSG code, when the file has none")] = None,
    encoding: Annotated[str | None, Form()] = None,
    x_column: Annotated[str | None, Form()] = None,
    y_column: Annotated[str | None, Form()] = None,
    z_column: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Register an uploaded file as a dataset.

    The dataset is owned by the uploader — the INSERT policy enforces that at
    the database, so this is not a privileged path.

    Warnings are part of the response, not a log line. A layer that lost forty
    null geometries on the way in has to say so where the person who uploaded
    it will see it.
    """
    from webmap_io.storage import StorageConfig, client

    storage = client(
        StorageConfig(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
            region=settings.s3_region,
        )
    )

    with tempfile.TemporaryDirectory(prefix="webmap-upload-") as workdir:
        source = await _spool(file, Path(workdir))
        options = IngestOptions(
            name=name,
            project_id=project_id,
            visibility=visibility,
            owner_team_id=owner_team_id,
            kind=kind,
            description=description,
            srid_override=srid,
            encoding=encoding,
            x_column=x_column,
            y_column=y_column,
            z_column=z_column,
        )

        if source.stat().st_size > INLINE_LIMIT_BYTES:
            # Spooled to object storage, not left in this container's temp
            # directory: the worker is a different container, and a path that
            # exists here is a file it cannot open.
            return await _enqueue_ingest(
                conn,
                principal,
                request,
                source,
                options,
                storage=storage,
                bucket=settings.s3_bucket,
            )

        result = await ingest_upload(
            conn,
            principal,
            source,
            options,
            storage=storage,
            bucket=settings.s3_bucket,
        )

    return {
        # Which of the two happened, so Claude knows whether to poll (§6) and
        # the SPA knows whether to show a progress bar or a layer.
        "mode": "inline",
        "dataset_id": str(result.dataset_id),
        "name": result.name,
        "kind": result.kind.value,
        "geometry_kind": result.geometry_kind.value,
        "srid": result.srid,
        "feature_count": result.feature_count,
        "bbox_4326": result.bbox_4326,
        "caption": result.caption,
        "warnings": result.warnings,
    }


__all__ = ["router"]


async def _enqueue_ingest(
    conn: Any,
    principal: Any,
    request: Request,
    source: Path,
    options: IngestOptions,
    *,
    storage: Any,
    bucket: str,
) -> dict[str, Any]:
    """Spool a large upload to object storage and hand it to the queue.

    The object is the source from here on, which is what lets an uploaded
    dataset be re-read by the same connector a share-sourced one uses
    (`11` §2.1) — and means the bytes a dataset came from are still there when
    somebody asks a year later what exactly was imported.
    """
    from uuid import uuid4

    from webmap_io.storage import put_file

    key = f"uploads/{uuid4()}/{source.name}"
    put_file(storage, bucket, key, source)

    parameters: dict[str, Any] = {
        "object_key": key,
        "name": options.name,
        "project_id": str(options.project_id) if options.project_id else None,
        "visibility": options.visibility.value,
        "owner_team_id": str(options.owner_team_id) if options.owner_team_id else None,
        "kind": options.kind.value if options.kind else None,
        "description": options.description,
        "srid_override": options.srid_override,
        "encoding": options.encoding,
        "x_column": options.x_column,
        "y_column": options.y_column,
        "z_column": options.z_column,
    }

    # Through the jobs route's own submitter, not a second enqueue path: row
    # first then queue is a property worth having in exactly one place.
    submitted = await submit_job(
        conn,
        principal,
        queue(request),
        kind="ingest",
        task="ingest_task",
        parameters=parameters,
    )

    return {
        "mode": "job",
        "job_id": str(submitted.job_id),
        "object_key": key,
        "size_bytes": source.stat().st_size,
        "reason": (
            f"{source.stat().st_size / 1e6:.0f} MB is past the {INLINE_LIMIT_BYTES / 1e6:.0f} MB "
            f"inline limit, so the read runs on the queue. Poll the job for the dataset."
        ),
    }
