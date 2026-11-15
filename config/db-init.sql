-- psql variables come from environment via \getenv, never command-line passwords.
-- format %I/%L quote identifiers/literals; fixed role/database names, no arbitrary SQL.
\set ON_ERROR_STOP on
\set ECHO none
\getenv app_password APP_PASSWORD
\getenv plugin_password PLUGIN_PASSWORD
SELECT 'CREATE ROLE dify_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='dify_owner') \gexec
SELECT 'CREATE ROLE dify_plugin_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='dify_plugin_owner') \gexec
-- Existing roles must have exactly the expected unprivileged trust boundary.
SELECT 1 / CASE WHEN count(*)=2 THEN 1 ELSE 0 END FROM pg_roles
 WHERE rolname IN ('dify_owner', 'dify_plugin_owner') AND rolcanlogin
 AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
SELECT 1 / CASE WHEN count(*)=0 THEN 1 ELSE 0 END FROM pg_auth_members
 WHERE member IN (SELECT oid FROM pg_roles WHERE rolname IN ('dify_owner', 'dify_plugin_owner'));
SELECT format('ALTER ROLE %I PASSWORD %L', 'dify_owner', :'app_password') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', 'dify_plugin_owner', :'plugin_password') \gexec
-- RDS administrator membership permits ownership assignment without app superuser.
GRANT dify_owner, dify_plugin_owner TO dify_admin;
SELECT 'CREATE DATABASE dify OWNER dify_owner'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='dify') \gexec
SELECT 'CREATE DATABASE dify_plugin OWNER dify_plugin_owner'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='dify_plugin') \gexec
REVOKE CONNECT ON DATABASE dify FROM PUBLIC;
REVOKE CONNECT ON DATABASE dify_plugin FROM PUBLIC;
-- Fail closed rather than adopting a differently owned database.
SELECT 1 / CASE WHEN count(*)=2 THEN 1 ELSE 0 END
 FROM pg_database d JOIN pg_roles r ON d.datdba=r.oid
 WHERE (d.datname='dify' AND r.rolname='dify_owner')
 OR (d.datname='dify_plugin' AND r.rolname='dify_plugin_owner');
