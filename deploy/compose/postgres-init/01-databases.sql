-- Extra databases on the shared local Postgres. The ADCE schema itself is created by Alembic (migrations/).
CREATE DATABASE langfuse;
CREATE DATABASE keycloak;
CREATE DATABASE adce_test;
\connect adce
CREATE EXTENSION IF NOT EXISTS vector;
