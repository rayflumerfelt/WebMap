"""Database sessions carrying RLS context. `03-auth-security.md` §3.4.

This module is the one file in the repository permitted to call
`engine.begin()` / `engine.connect()` — the pre-commit `no-raw-db-session`
hook excludes exactly this path (`CLAUDE.md` §11). A session opened anywhere
else has no `webmap.user_id` set, so every RLS policy evaluates against a
missing setting and the query errors or, worse, matches nothing and looks
like an empty result.

It lives in `webmap_core` rather than in the API because **the worker needs
principal-scoped connections too**. A job runs long after its request is
gone, and §3.2's rule — never create a session without a `Principal` — has to
hold there as well. The alternative was a second `engine.begin()` in the
worker, which is exactly the thing the hook exists to prevent.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from webmap_core.logging import get_logger
from webmap_core.permissions import Principal

log = get_logger(__name__)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        # The control plane holds small transactional rows and ~5-10 users.
        # A large pool buys nothing and makes a connection leak take longer
        # to notice.
        pool_size=10,
        max_overflow=5,
    )


@asynccontextmanager
async def principal_session(
    engine: AsyncEngine, principal: Principal
) -> AsyncIterator[AsyncConnection]:
    """Every DB session used to serve a request must go through this.

    `set_config(..., true)` makes the setting **transaction-local**, so it
    cannot leak across pooled connections. Using `SET` without the local flag
    is a serious bug: the next request to borrow that connection inherits the
    previous user's RLS context and reads their rows.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('webmap.user_id', :uid, true)"),
            {"uid": str(principal.user_id)},
        )
        await conn.execute(
            text("SELECT set_config('webmap.team_ids', :tids, true)"),
            {"tids": principal.team_ids_literal},
        )
        yield conn


@asynccontextmanager
async def unscoped_session(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A session with no principal, for work that precedes authentication.

    Legitimate uses are few and named: the startup RLS assertion, the OIDC
    login upsert (there is no principal yet — the user is being created), and
    health checks. Anything that reads owned rows belongs in
    `principal_session` instead.

    Tables read here are not RLS-protected; if you find yourself reaching for
    this to read `dataset`, that is the bug.
    """
    async with engine.begin() as conn:
        yield conn


async def assert_rls_enforced(engine: AsyncEngine) -> None:
    """Fail loudly at boot if the app role can bypass RLS.

    `02-data-model.md` §4: the application role must not have BYPASSRLS, and
    migrations run as a separate privileged role. If someone grants it — to
    debug a query, usually — every policy in the database becomes inert and
    nothing else would say so.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
        row = result.one_or_none()
        if row is None:
            raise RuntimeError(
                "Could not read pg_roles for the current user. The application "
                "role must be a real role with BYPASSRLS verifiably unset; "
                "refusing to start without confirming it."
            )
        role_name, bypasses = row
        if bypasses:
            raise RuntimeError(
                f"Application DB role '{role_name}' has BYPASSRLS. All "
                f"row-level security is inert. Revoke it "
                f"(ALTER ROLE {role_name} NOBYPASSRLS) and restart. "
                f"Refusing to start."
            )

    log.info("rls_assertion_passed", role=role_name)


async def assert_policies_present(engine: AsyncEngine) -> None:
    """Verify every ownable table actually has RLS enabled and a policy.

    The BYPASSRLS check above proves the role cannot bypass policies. It does
    not prove there *are* any — a table created by a migration that forgot
    `ENABLE ROW LEVEL SECURITY` is wide open and passes the first assertion.
    """
    expected = {
        "project",
        "dataset",
        "style_template",
        "palette",
        "map_session",
        "render",
    }
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, count(p.polname) AS policies
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_policy p ON p.polrelid = c.oid
                WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
                GROUP BY c.relname, c.relrowsecurity
                """
            ),
            {"tables": sorted(expected)},
        )
        state = {name: (enabled, count) for name, enabled, count in result}

    missing = sorted(expected - set(state))
    unprotected = sorted(
        name for name, (enabled, count) in state.items() if not enabled or count == 0
    )
    if missing or unprotected:
        raise RuntimeError(
            f"Row-level security is not in place. Missing tables: "
            f"{missing or 'none'}. Tables without RLS enabled or without any "
            f"policy: {unprotected or 'none'}. Run migrations; if they are "
            f"current, a migration added an ownable table without the RLS "
            f"block (02-data-model.md §4). Refusing to start."
        )
