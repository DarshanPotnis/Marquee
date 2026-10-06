-- Runs once, when the container's data directory is first created. POSTGRES_DB makes marquee_test
-- (for the tests); this adds marquee, the database a local pipeline run writes to.
CREATE DATABASE marquee;
