-- Two roles, deliberately different. `02-data-model.md` §4.
--
--   webmap_app       the application connects as this. It must NOT have
--                    BYPASSRLS — the API asserts that at startup and refuses
--                    to boot if it does (03-auth-security.md §3.5).
--   webmap_migrator  runs Alembic. It owns the tables, so it needs the
--                    privileges the application must not have.
--
-- Passwords here are local-stack only. Production takes them from the
-- corporate secret manager, never from a file in the image (03 §11).

CREATE ROLE webmap_migrator LOGIN PASSWORD 'webmap' CREATEROLE;
CREATE ROLE webmap_app LOGIN PASSWORD 'webmap' NOBYPASSRLS;

GRANT ALL ON DATABASE webmap TO webmap_migrator;

-- The migrator owns the public schema so it can create tables and policies.
ALTER SCHEMA public OWNER TO webmap_migrator;
GRANT USAGE ON SCHEMA public TO webmap_app;

-- Table privileges for objects the migrator has not created yet. Without
-- this, every migration would need a matching GRANT and one will be
-- forgotten; the failure looks like a permission error at runtime, long
-- after the migration passed.
ALTER DEFAULT PRIVILEGES FOR ROLE webmap_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO webmap_app;
ALTER DEFAULT PRIVILEGES FOR ROLE webmap_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO webmap_app;

-- Belt and braces: NOBYPASSRLS is the default, but state it and prove it.
DO $$
BEGIN
    IF (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'webmap_app') THEN
        RAISE EXCEPTION 'webmap_app has BYPASSRLS; all row-level security '
                        'would be inert. Refusing to initialise.';
    END IF;
END
$$;
