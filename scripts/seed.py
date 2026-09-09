"""Seed the local stack with synthetic Midland Basin data.

`12-roadmap.md` Phase 0: 2,000 points, 20 faults, and one grid in EPSG:2277
(NAD83 / Texas Central, ftUS) — the working CRS for the Midland Basin, chosen
because that is what the data is in and not because we guessed
(`02-data-model.md` §1).

Everything here is synthetic. `CLAUDE.md` §7.5: no real data in the
repository, and `tests/fixtures/` is synthetic only.

    uv run python scripts/seed.py

Idempotent: re-running replaces the seeded rows and objects rather than
accumulating duplicates.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import numpy as np
import shapely
from numpy.typing import NDArray

from webmap_core.settings import get_settings
from webmap_geo.crs import WGS84, transform_bbox
from webmap_io.parquet import write_features
from webmap_io.raster import Affine, grid_transform, write_cog
from webmap_io.storage import (
    StorageConfig,
    client,
    ensure_bucket,
    feature_key,
    grid_key,
    put_file,
)

# NAD83 / Texas Central (ftUS).
ANALYSIS_SRID = 2277

#: Deterministic ids, so re-seeding updates the same rows and a session or
#: lineage record written against a seeded dataset survives a re-seed.
NAMESPACE = UUID("6f1a5c9e-0000-4000-8000-000000000000")

#: A rectangle over the Midland Basin, in EPSG:2277 feet — about 106 x 76
#: miles, Midland and Odessa across to Big Spring. Verified by transforming
#: back: it lands at roughly (-102.91, 31.17)-(-101.09, 32.30) in EPSG:4326.
#:
#: An earlier revision used a northing near 6.6M ftUS, inferred from this
#: projection's false-northing constant rather than checked against a known
#: point. That put the whole seed 1,200 km south, in central Mexico, and
#: nothing failed — the grids were internally consistent and the registry
#: bbox agreed with them. It is the exact failure 02-data-model.md §1 names,
#: which is why test_seed_extent.py now asserts this against real towns.
EXTENT_FT = (1_500_000.0, 10_400_000.0, 2_060_000.0, 10_800_000.0)

#: One seed for the whole script. CLAUDE.md §3.3: all randomness takes an
#: explicit Generator — no bare np.random, no module-level seeding.
SEED = 20260908

WOLFCAMP_VINTAGE = date(2026, 7, 31)


def sid(name: str) -> UUID:
    return uuid5(NAMESPACE, name)


@dataclass(frozen=True)
class SeededDataset:
    id: UUID
    name: str
    kind: str
    geometry_kind: str | None
    storage_key: str
    feature_count: int | None
    bbox_ft: tuple[float, float, float, float]
    caption: str


# --- Synthetic geology ------------------------------------------------------


def porosity_field(
    x: NDArray[np.float64], y: NDArray[np.float64], rng: np.random.Generator
) -> NDArray[np.float64]:
    """A plausible porosity surface: regional trend plus two anomalies.

    Not a geological model — it exists so the seeded map looks like something
    a geologist would recognise rather than white noise, and so contouring and
    kriging have a field with real structure to recover. Porosity increases
    basinward (to the east-southeast here) with two local highs.
    """
    west, south, east, north = EXTENT_FT
    u = (x - west) / (east - west)
    v = (y - south) / (north - south)

    regional = 6.0 + 9.0 * u - 3.0 * v
    high_a = 6.5 * np.exp(-(((u - 0.30) / 0.11) ** 2 + ((v - 0.62) / 0.13) ** 2))
    high_b = 4.5 * np.exp(-(((u - 0.72) / 0.09) ** 2 + ((v - 0.28) / 0.10) ** 2))
    noise = rng.normal(0.0, 0.45, size=x.shape)

    # Porosity is a fraction of rock volume; a negative one is not a low
    # value, it is a bug that would propagate into a variogram.
    return np.clip(regional + high_a + high_b + noise, 0.5, None)


def control_points(rng: np.random.Generator, n: int = 2_000) -> dict[str, Any]:
    """Well control, clustered the way development actually clusters.

    Two thirds sit in a handful of pads and one third is scattered. That
    matters downstream: declustering exists because well control clusters by
    development history rather than by geology (`CLAUDE.md` §13), and a
    uniformly random seed dataset would make declustering look pointless.
    """
    west, south, east, north = EXTENT_FT
    n_clustered = (n * 2) // 3
    n_scattered = n - n_clustered

    pads = 8
    pad_x = rng.uniform(west + 20_000, east - 20_000, pads)
    pad_y = rng.uniform(south + 20_000, north - 20_000, pads)
    which = rng.integers(0, pads, n_clustered)
    cx = rng.normal(pad_x[which], 9_000)
    cy = rng.normal(pad_y[which], 9_000)

    sx = rng.uniform(west, east, n_scattered)
    sy = rng.uniform(south, north, n_scattered)

    x = np.clip(np.concatenate([cx, sx]), west, east)
    y = np.clip(np.concatenate([cy, sy]), south, north)
    porosity = porosity_field(x, y, rng)
    # Structure: a gently dipping surface, TVDSS negative below sea level.
    tvdss = -(8_200.0 + 0.004 * (x - west) - 0.002 * (y - south))

    props = [
        {
            "well_name": f"SYN {i // 100 + 1}-{i % 100 + 1}H",
            "api14": f"42{329 + (i % 7):03d}{100000 + i:06d}",
            "porosity": round(float(p), 2),
            "tvdss": round(float(z), 1),
            "formation": "Wolfcamp A",
        }
        for i, (p, z) in enumerate(zip(porosity, tvdss, strict=True))
    ]
    return {"geometry": shapely.points(x, y), "props": props, "x": x, "y": y}


def fault_network(rng: np.random.Generator, n: int = 20) -> dict[str, Any]:
    """Twenty fault traces on a dominant northwest-southeast trend.

    Real fault sets have a preferred orientation from the regional stress
    field; drawing 20 random azimuths would produce a spaghetti network that
    triangulates badly and teaches nothing about the constraint handling this
    project exists for.
    """
    west, south, east, north = EXTENT_FT
    traces: list[Any] = []
    props: list[dict[str, Any]] = []

    for i in range(n):
        # Azimuth clustered around 135 degrees (NW-SE), the dominant trend.
        azimuth = np.radians(rng.normal(135.0, 12.0))
        length = rng.uniform(38_000, 105_000)
        x0 = rng.uniform(west + 15_000, east - 15_000)
        y0 = rng.uniform(south + 15_000, north - 15_000)

        # A gently curving trace: three segments with a small azimuth drift,
        # so the constrained triangulation sees something other than a
        # straight edge.
        steps = 4
        xs = [x0]
        ys = [y0]
        drift = np.radians(rng.normal(0.0, 4.0))
        for step in range(steps):
            heading = azimuth + drift * step
            xs.append(xs[-1] + (length / steps) * np.sin(heading))
            ys.append(ys[-1] + (length / steps) * np.cos(heading))

        traces.append(shapely.linestrings(np.column_stack([xs, ys])))
        # A quarter are breaklines: value continuous across them, gradient
        # discontinuous, and they carry their own Z (`CLAUDE.md` §13).
        is_breakline = i % 4 == 3
        props.append(
            {
                "constraint_kind": "breakline" if is_breakline else "fault",
                "name": f"Synthetic Fault {i + 1:02d}",
                "throw_ft": None if is_breakline else round(float(rng.uniform(40, 320)), 1),
                "z_values": None,
            }
        )

    return {"geometry": np.array(traces, dtype=object), "props": props}


def gridded_surface(
    rng: np.random.Generator, cell_size: float = 500.0
) -> tuple[NDArray[np.float64], Affine, tuple[float, float]]:
    """The seeded grid. A 500 ft cell over the extent, from the same field."""
    west, south, east, north = EXTENT_FT
    nx = int((east - west) // cell_size)
    ny = int((north - south) // cell_size)

    # Cell centres. Rasters are north-up: row 0 is the *top*, so the y axis
    # runs down from `north`. Getting this backwards flips the map vertically
    # and looks entirely plausible until someone checks a well against it.
    xs = west + (np.arange(nx) + 0.5) * cell_size
    ys = north - (np.arange(ny) + 0.5) * cell_size
    gx, gy = np.meshgrid(xs, ys)

    grid = porosity_field(gx, gy, rng)
    # A nodata corner, so nodata handling is exercised by the seed rather
    # than discovered in production.
    grid[: ny // 12, : nx // 12] = np.nan

    transform = grid_transform(west, north, cell_size)
    return grid, transform, (float(np.nanmin(grid)), float(np.nanmax(grid)))


# --- Writing ---------------------------------------------------------------


def bbox_4326(bbox_ft: tuple[float, float, float, float]) -> list[float]:
    return list(transform_bbox(bbox_ft, ANALYSIS_SRID, WGS84))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write objects to this directory instead of uploading to object "
        "storage. Useful for inspecting what the seed produces without a "
        "running stack.",
    )
    parser.add_argument("--points", type=int, default=2_000)
    parser.add_argument("--faults", type=int, default=20)
    args = parser.parse_args(argv)

    rng = np.random.default_rng(SEED)
    settings = get_settings()

    points = control_points(rng, args.points)
    faults = fault_network(rng, args.faults)
    grid, transform, (vmin, vmax) = gridded_surface(rng)

    staging = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="webmap-seed-"))
    staging.mkdir(parents=True, exist_ok=True)

    points_id = sid("dataset:wolfcamp-a-control-points")
    faults_id = sid("dataset:midland-fault-network")
    grid_id = sid("dataset:wolfcamp-a-porosity-grid")

    points_path = staging / "points.parquet"
    points_result = write_features(
        points_path,
        geometry=points["geometry"],
        props=points["props"],
        srid=ANALYSIS_SRID,
    )
    faults_path = staging / "faults.parquet"
    faults_result = write_features(
        faults_path,
        geometry=faults["geometry"],
        props=faults["props"],
        srid=ANALYSIS_SRID,
    )
    grid_path = staging / "porosity.tif"
    write_cog(grid, transform, ANALYSIS_SRID, grid_path)

    seeded = [
        SeededDataset(
            id=points_id,
            name="Wolfcamp A Control Points",
            kind="pointset",
            geometry_kind="point",
            storage_key=feature_key(str(points_id), 1),
            feature_count=points_result.feature_count,
            bbox_ft=points_result.bbox,
            caption=(
                f"{points_result.feature_count:,} synthetic well control points "
                f"with Wolfcamp A porosity and TVDSS, EPSG:2277."
            ),
        ),
        SeededDataset(
            id=faults_id,
            name="Midland Basin Fault Network",
            kind="fault_network",
            geometry_kind="linestring",
            storage_key=feature_key(str(faults_id), 1),
            feature_count=faults_result.feature_count,
            bbox_ft=faults_result.bbox,
            caption=(
                f"{faults_result.feature_count} synthetic fault traces on a "
                f"NW-SE trend; every fourth is a breakline."
            ),
        ),
        SeededDataset(
            id=grid_id,
            name="Wolfcamp A Porosity Grid",
            kind="grid",
            geometry_kind=None,
            storage_key=grid_key(str(grid_id)),
            feature_count=None,
            bbox_ft=EXTENT_FT,
            caption=(
                f"Synthetic Wolfcamp A porosity, 500 ft cells, "
                f"{vmin:.1f}-{vmax:.1f}%, EPSG:2277."
            ),
        ),
    ]

    if args.out:
        manifest = staging / "manifest.json"
        manifest.write_text(
            json.dumps(
                [
                    {
                        "id": str(d.id),
                        "name": d.name,
                        "kind": d.kind,
                        "key": d.storage_key,
                        "feature_count": d.feature_count,
                        "bbox_4326": bbox_4326(d.bbox_ft),
                    }
                    for d in seeded
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        _report(seeded, points_result.row_groups, faults_result.row_groups, staging)
        return 0

    storage = StorageConfig(
        endpoint=settings.s3_endpoint,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
    )
    s3 = client(storage)
    ensure_bucket(s3, storage.bucket)
    for dataset, path in zip(seeded, [points_path, faults_path, grid_path], strict=True):
        put_file(s3, storage.bucket, dataset.storage_key, path)

    _register(seeded, vmin, vmax, settings.migration_database_url)
    _report(seeded, points_result.row_groups, faults_result.row_groups, staging)
    return 0


def _register(seeded: list[SeededDataset], vmin: float, vmax: float, database_url: str) -> None:
    """Insert the registry rows.

    Connects with the **migration** role because it owns the tables, but that
    buys no exemption: the migration sets FORCE ROW LEVEL SECURITY, so policies
    apply to the owner too. The seed therefore establishes an RLS context the
    same way a request does (`03-auth-security.md` §3.4) before touching any
    ownable table.

    That is the right shape rather than a workaround. It means the seed
    exercises the INSERT policies instead of going around them, so a policy
    that forbids legitimate writes fails here rather than in Phase 1.

    `app_user`, `team` and `team_member` are not ownable and carry no policies,
    which is what makes the ordering possible — the principal has to exist
    before it can be declared.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(
        database_url.replace("postgresql+psycopg://", "postgresql://")
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_user (id, subject, email, display_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (subject) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """,
                (
                    sid("user:seed"),
                    "seed|local-development",
                    "seed@webmap.local",
                    "Seed Data Owner",
                ),
            )
            row = cur.fetchone()
            assert row is not None
            owner_id = row[0]

            cur.execute(
                """
                INSERT INTO team (id, slug, display_name, idp_group_id)
                VALUES (%s, 'permian-asset', 'Permian Asset Team', 'seed-group-permian')
                ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
                """,
                (sid("team:permian-asset"),),
            )
            row = cur.fetchone()
            assert row is not None
            team_id = row[0]

            # A second team nobody in the seed belongs to. It exists so the
            # local stack can demonstrate team-scoped visibility by hand: the
            # dev roster puts Alan here and Ada and Grace on Permian, which is
            # the shape "User A cannot read User B's data" needs.
            cur.execute(
                """
                INSERT INTO team (id, slug, display_name, idp_group_id)
                VALUES (%s, 'exploration', 'Exploration Team',
                        'seed-group-exploration')
                ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
                """,
                (sid("team:exploration"),),
            )

            cur.execute(
                "INSERT INTO team_member (team_id, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (team_id, owner_id),
            )

            # Everything below writes to RLS-protected tables. Declare the
            # principal, transaction-local, exactly as principal_session does.
            # Without this the policies raise "unrecognized configuration
            # parameter" — deliberately loud, because the alternative is a
            # seed that silently inserts nothing and reports success.
            cur.execute("SELECT set_config('webmap.user_id', %s, true)", (str(owner_id),))
            cur.execute(
                "SELECT set_config('webmap.team_ids', %s, true)",
                ("{" + str(team_id) + "}",),
            )

            project_id = sid("project:midland-basin")
            cur.execute(
                """
                INSERT INTO project (
                    id, slug, name, description, analysis_srid, horizontal_unit,
                    vertical_unit, depth_positive_down, default_extent,
                    owner_user_id, owner_team_id, visibility)
                VALUES (%s, 'midland-basin', 'Midland Basin', %s, %s, 'usft',
                        'ft', TRUE, %s, %s, %s, 'team')
                ON CONFLICT (slug) DO UPDATE SET updated_at = now()
                """,
                (
                    project_id,
                    "Synthetic seed project. EPSG:2277 (NAD83 / Texas Central, ftUS).",
                    ANALYSIS_SRID,
                    bbox_4326(EXTENT_FT),
                    owner_id,
                    team_id,
                ),
            )

            for dataset in seeded:
                is_grid = dataset.kind == "grid"
                cur.execute(
                    """
                    INSERT INTO dataset (
                        id, project_id, name, kind, geometry_kind, connector,
                        storage_srid, bbox_4326, parquet_key, version,
                        feature_count, attribute_schema, cog_key, grid_cell_size,
                        value_min, value_max, value_unit, vertical_unit,
                        data_vintage, caption, owner_user_id, owner_team_id,
                        visibility)
                    VALUES (%s, %s, %s, %s, %s, 'upload', %s, %s, %s, 1, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'team')
                    ON CONFLICT (id) DO UPDATE SET
                        caption = EXCLUDED.caption,
                        feature_count = EXCLUDED.feature_count,
                        bbox_4326 = EXCLUDED.bbox_4326,
                        updated_at = now()
                    """,
                    (
                        dataset.id,
                        project_id,
                        dataset.name,
                        dataset.kind,
                        dataset.geometry_kind,
                        ANALYSIS_SRID,
                        bbox_4326(dataset.bbox_ft),
                        None if is_grid else dataset.storage_key,
                        dataset.feature_count,
                        Jsonb(_attribute_schema(dataset.kind)) if not is_grid else None,
                        dataset.storage_key if is_grid else None,
                        500.0 if is_grid else None,
                        vmin if is_grid else None,
                        vmax if is_grid else None,
                        "%" if is_grid else None,
                        "ft" if is_grid else None,
                        WOLFCAMP_VINTAGE,
                        dataset.caption,
                        owner_id,
                        team_id,
                    ),
                )
                if not is_grid:
                    cur.execute(
                        """
                        INSERT INTO dataset_version (
                            dataset_id, version, parquet_key, feature_count, created_by)
                        VALUES (%s, 1, %s, %s, %s)
                        ON CONFLICT (dataset_id, version) DO NOTHING
                        """,
                        (dataset.id, dataset.storage_key, dataset.feature_count, owner_id),
                    )
                if dataset.kind == "fault_network":
                    cur.execute(
                        """
                        INSERT INTO fault_network (id, dataset_id, is_validated)
                        VALUES (%s, %s, FALSE)
                        ON CONFLICT (dataset_id) DO NOTHING
                        """,
                        (sid("fault_network:midland"), dataset.id),
                    )

            cur.execute(
                """
                INSERT INTO audit_event (actor_user_id, actor_channel, action, detail)
                VALUES (%s, 'web', 'seed.load', %s)
                """,
                (
                    owner_id,
                    Jsonb(
                        {
                            "datasets": [str(d.id) for d in seeded],
                            "at": datetime.now(UTC).isoformat(),
                        }
                    ),
                ),
            )
        conn.commit()


def _attribute_schema(kind: str) -> list[dict[str, Any]]:
    if kind == "fault_network":
        return [
            {
                "name": "constraint_kind",
                "type": "string",
                "nullable": False,
                "description": "'fault' (hard, value discontinuous) or 'breakline' "
                "(soft, gradient discontinuous)",
            },
            {"name": "name", "type": "string", "nullable": False},
            {
                "name": "throw_ft",
                "type": "number",
                "nullable": True,
                "description": "Vertical displacement across the fault, feet",
            },
        ]
    return [
        {"name": "well_name", "type": "string", "nullable": False},
        {"name": "api14", "type": "string", "nullable": False},
        {
            "name": "porosity",
            "type": "number",
            "nullable": False,
            "description": "Wolfcamp A porosity, percent",
        },
        {
            "name": "tvdss",
            "type": "number",
            "nullable": False,
            "description": "True vertical depth subsea, feet, negative below sea level",
        },
        {"name": "formation", "type": "string", "nullable": False},
    ]


def _report(
    seeded: list[SeededDataset], point_groups: int, fault_groups: int, staging: Path
) -> None:
    lines = [f"Seeded {len(seeded)} datasets in EPSG:{ANALYSIS_SRID}:"]
    for dataset in seeded:
        count = f"{dataset.feature_count:,} features" if dataset.feature_count else "grid"
        lines.append(f"  {dataset.name:<34} {count:>16}  {dataset.storage_key}")
    lines.append(
        f"Row groups: points={point_groups}, faults={fault_groups} "
        f"(Hilbert-ordered; see 11-file-io.md §6.1)"
    )
    lines.append(f"Staged in {staging}")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
