DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='edm') THEN
    CREATE ROLE edm LOGIN PASSWORD 'edm';
  END IF;
END
$$;
SELECT 'role_ok' AS status;
