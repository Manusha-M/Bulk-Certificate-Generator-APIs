-- Runs once, when the PostgreSQL volume is first created.
-- The test suite uses this database so it never touches the development data.
CREATE DATABASE certificates_test;
