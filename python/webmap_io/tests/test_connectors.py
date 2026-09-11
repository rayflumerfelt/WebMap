"""Connectors. `11-file-io.md` §2.

The tests that matter here are the security ones. A file share connector is the
awkward part of a multi-user deployment — the service account can read whatever
the share exposes — so the boundary of what it will resolve is the whole of its
safety, and `03-auth-security.md` treats a path escaping its root as a
vulnerability rather than a bug.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from webmap_io.connectors.base import PathTraversal, SourceDescriptor, UnknownSource
from webmap_io.connectors.fileshare import (
    FileShareConnector,
    ShareConfig,
    checksum_of,
    parse_share_uri,
)
from webmap_io.connectors.upload import UploadConnector, parse_object_uri

TEAM = uuid4()


@pytest.fixture
def share(tmp_path: Path) -> FileShareConnector:
    root = tmp_path / "picks"
    (root / "2026").mkdir(parents=True)
    (root / "2026" / "wolfcamp.csv").write_text("x,y,z\n1,2,3\n", encoding="utf-8")
    (root / "2026" / "notes.docx").write_bytes(b"not geospatial")
    (root / "leases.shp").write_bytes(b"shp")
    (root / "leases.dbf").write_bytes(b"dbf")
    (root / "leases.prj").write_bytes(b"prj")

    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "payroll.csv").write_text("do not read", encoding="utf-8")

    return FileShareConnector({"picks": ShareConfig(root=root, team_id=TEAM)})


class TestUriParsing:
    def test_splits_a_share_uri(self) -> None:
        assert parse_share_uri("share://picks/2026/wolfcamp.shp") == (
            "picks",
            "2026/wolfcamp.shp",
        )

    def test_refuses_a_bare_path_and_says_what_one_looks_like(self) -> None:
        """A bare path in the database is indistinguishable from a local one,
        and there is no way to tell later which root it was relative to."""
        with pytest.raises(UnknownSource, match="share://picks/"):
            parse_share_uri("/mnt/picks/2026/wolfcamp.shp")


class TestPathTraversal:
    async def test_refuses_a_path_that_climbs_out(self, share: FileShareConnector) -> None:
        with pytest.raises(PathTraversal, match="outside share"):
            await share.describe("share://picks/../secrets/payroll.csv")

    @pytest.mark.skipif(
        sys.platform == "win32", reason="creating a symlink needs elevation on Windows"
    )
    async def test_refuses_a_symlink_that_points_out(
        self, share: FileShareConnector, tmp_path: Path
    ) -> None:
        """The interesting half of the attack. An unresolved comparison passes
        this — the path looks like it is inside the share right up until it is
        opened."""
        (tmp_path / "picks" / "escape.csv").symlink_to(tmp_path / "secrets" / "payroll.csv")

        with pytest.raises(PathTraversal):
            await share.describe("share://picks/escape.csv")

    async def test_refuses_an_unconfigured_share_and_names_the_configured_ones(
        self, share: FileShareConnector
    ) -> None:
        """A share not in the map is not reachable — mitigation 2. Adding one
        is a deployment change, and the message says so."""
        with pytest.raises(UnknownSource, match="Configured shares: picks"):
            await share.describe("share://payroll/anything.csv")


class TestListing:
    async def test_lists_configured_shares_when_asked_for_nothing(
        self, share: FileShareConnector
    ) -> None:
        listed = await share.list()

        assert [item.name for item in listed] == ["picks"]

    async def test_lists_only_readable_formats(self, share: FileShareConnector) -> None:
        """A share holds years of spreadsheets and documents. Listing all of
        them turns a source picker into a file browser and buries the four
        files anybody wants."""
        names = {item.name for item in await share.list("share://picks/")}

        assert "wolfcamp.csv" in names
        assert "leases.shp" in names
        assert "notes.docx" not in names

    async def test_a_listing_is_stable(self, share: FileShareConnector) -> None:
        """An unsorted listing changes order between calls for no reason a user
        can see, and a picker that reshuffles is one people stop trusting."""
        first = [item.uri for item in await share.list("share://picks/")]
        second = [item.uri for item in await share.list("share://picks/")]

        assert first == second == sorted(first)


class TestDescribe:
    async def test_reports_size_and_checksum(self, share: FileShareConnector) -> None:
        path = Path(share._resolve("share://picks/2026/wolfcamp.csv"))
        described = await share.describe("share://picks/2026/wolfcamp.csv")

        # Against the file rather than a literal: text written on Windows picks
        # up CRLF, and a hard-coded byte count makes this a test of the
        # platform's line endings.
        assert described.size_bytes == path.stat().st_size
        assert described.checksum == checksum_of(path)

    async def test_says_so_when_the_file_is_gone(self, share: FileShareConnector) -> None:
        with pytest.raises(UnknownSource, match="moved or renamed"):
            await share.describe("share://picks/2026/missing.csv")


class TestChange:
    async def test_unchanged_content_is_unchanged(self, share: FileShareConnector) -> None:
        described = await share.describe("share://picks/2026/wolfcamp.csv")

        assert not await share.has_changed(
            "share://picks/2026/wolfcamp.csv", described.checksum
        )

    async def test_edited_content_has_changed(
        self, share: FileShareConnector, tmp_path: Path
    ) -> None:
        described = await share.describe("share://picks/2026/wolfcamp.csv")
        (tmp_path / "picks" / "2026" / "wolfcamp.csv").write_text(
            "x,y,z\n1,2,4\n", encoding="utf-8"
        )

        assert await share.has_changed("share://picks/2026/wolfcamp.csv", described.checksum)

    async def test_a_restored_backup_does_not_look_changed(
        self, share: FileShareConnector, tmp_path: Path
    ) -> None:
        """Content, not mtime. A share restored from backup has new mtimes and
        identical bytes; re-ingesting everything on a restore is slow and leaves
        a pile of versions nobody asked for."""
        path = tmp_path / "picks" / "2026" / "wolfcamp.csv"
        described = await share.describe("share://picks/2026/wolfcamp.csv")
        path.touch()

        assert not await share.has_changed(
            "share://picks/2026/wolfcamp.csv", described.checksum
        )

    async def test_never_having_read_it_counts_as_changed(
        self, share: FileShareConnector
    ) -> None:
        """A sync that wrongly re-reads costs time; one that wrongly skips
        leaves a geologist looking at last month's picks believing they are
        current."""
        assert await share.has_changed("share://picks/2026/wolfcamp.csv", None)


class TestFetch:
    async def test_brings_the_shapefile_sidecars(
        self, share: FileShareConnector, tmp_path: Path
    ) -> None:
        """A shapefile is five files. Fetching only the `.shp` gives a file that
        reads as empty rather than as broken, and without the `.prj` there is no
        CRS — which `CLAUDE.md` §3.1 forbids inferring."""
        dest = tmp_path / "work"

        fetched = await share.fetch("share://picks/leases.shp", dest)

        assert fetched.name == "leases.shp"
        assert {path.name for path in dest.iterdir()} == {
            "leases.shp",
            "leases.dbf",
            "leases.prj",
        }

    async def test_returns_the_file_to_open_not_the_directory(
        self, share: FileShareConnector, tmp_path: Path
    ) -> None:
        fetched = await share.fetch("share://picks/2026/wolfcamp.csv", tmp_path / "work")

        assert fetched.is_file()
        assert fetched.read_text(encoding="utf-8").startswith("x,y,z")


class TestOwnership:
    async def test_a_share_names_the_team_that_will_own_its_data(
        self, share: FileShareConnector
    ) -> None:
        """Mitigation 3: registration sets `owner_team_id` from the mapping, so
        ordinary object permissions apply from ingest onward rather than being
        bolted on afterwards."""
        assert share.team_for("share://picks/2026/wolfcamp.csv") == TEAM


class TestUploadConnector:
    def test_splits_an_object_uri(self) -> None:
        assert parse_object_uri("s3://webmap/uploads/abc/picks.csv") == (
            "webmap",
            "uploads/abc/picks.csv",
        )

    async def test_an_upload_never_changes(self) -> None:
        """The object it names is immutable, so a sync of an upload-sourced
        dataset records the time and does nothing else."""
        connector = UploadConnector(store=None)

        assert not await connector.has_changed("s3://webmap/uploads/abc/picks.csv", None)

    async def test_uploads_are_not_browsable(self) -> None:
        """An upload belongs to the dataset it created. Listing the bucket would
        offer one user another user's file."""
        assert await UploadConnector(store=None).list() == []

    async def test_describes_without_reaching_storage(self) -> None:
        described = await UploadConnector(store=None).describe(
            "s3://webmap/uploads/abc/picks.csv"
        )

        assert described == SourceDescriptor(
            uri="s3://webmap/uploads/abc/picks.csv",
            name="picks.csv",
            format_hint="csv",
        )
