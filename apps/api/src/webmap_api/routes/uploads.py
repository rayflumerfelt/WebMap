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
from webmap_core.models import DatasetKind
from webmap_core.permissions import Visibility
from webmap_core.services.ingest import IngestOptions, ingest_upload, preview

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])

#: Read in chunks rather than `await file.read()`. The size cap is enforced in
#: the pipeline, but a single read would have to hold the whole upload in
#: memory first, which is how the cap gets reached by the API rather than by
#: the file.
CHUNK_BYTES = 1024 * 1024


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
        result = await ingest_upload(
            conn,
            principal,
            source,
            IngestOptions(
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
            ),
            storage=storage,
            bucket=settings.s3_bucket,
        )

    return {
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
