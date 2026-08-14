# Database Migrations

Manual migration system for Musica database.

## Usage

Migrations are automatically applied when the database is initialized. Each migration file should be named with a number prefix (e.g., `001_initial_schema.sql`).

## Creating a New Migration

1. Create a new SQL file in this directory with the next number
2. Write the SQL to modify the schema
3. The migration will be applied automatically on next startup

## Migration Files

- `001_initial_schema.sql` — Initial database schema (all tables)
