# Database Migrations

Word App uses [Alembic](https://alembic.sqlalchemy.org/) for database schema version control and migrations. This ensures that your vocabulary, progress, and audio assets are preserved across app updates.

## Overview

- **Database**: SQLite (stored in `data/vocabulary.db`)
- **Migration Tool**: Alembic
- **Initial Schema**: Version `30fe5c606d36` (initial schema)

## Getting Started

### For New Installations

If you're setting up Word App for the first time, run:

```bash
alembic upgrade head
```

This will create all database tables based on the initial migration.

### For Existing Databases

If you already have a Word App database with data:

1. The initial migration checks for existing tables and won't recreate them
2. Simply run: `alembic upgrade head` to mark the current schema as version-controlled
3. Your existing data will be preserved

## Creating a Migration

When you need to modify the database schema:

1. **Update the SQLAlchemy models** in `app/models/`

2. **Generate a new migration**:
   ```bash
   alembic revision --autogenerate -m "description of changes"
   ```

3. **Review the generated migration** in `alembic/versions/`:
   - Check that it only includes the changes you want
   - Ensure it won't drop or modify existing data unintentionally
   - Test on a backup database first

4. **Test the migration**:
   ```bash
   # Test upgrade
   alembic upgrade head
   
   # Test downgrade (if needed)
   alembic downgrade -1
   ```

5. **Apply to production**:
   ```bash
   alembic upgrade head
   ```

## Upgrading the Database

To apply all pending migrations:

```bash
alembic upgrade head
```

To upgrade to a specific revision:

```bash
alembic upgrade <revision_id>
```

## Downgrading the Database

To rollback one migration:

```bash
alembic downgrade -1
```

To rollback to a specific revision:

```bash
alembic downgrade <revision_id>
```

## Checking Migration Status

To see the current database version:

```bash
alembic current
```

To see all available migrations:

```bash
alembic history
```

## Important Rules

1. **Never use `Base.metadata.create_all()` in production**
   - Schema is managed by Alembic migrations only
   - The `create_tables()` function has been disabled in app startup

2. **Always backup before migrating**
   - Copy `data/vocabulary.db` before running migrations
   - Export your data using the export feature before major schema changes

3. **Test migrations on a copy first**
   - Use a backup database to test migrations
   - Verify that existing data remains intact

4. **Don't modify applied migrations**
   - Once a migration is applied to production, don't edit it
   - Create a new migration to fix any issues

5. **Preserve existing data**
   - Initial migration checks for existing tables
   - Always ensure migrations are non-destructive

## Migration Best Practices

1. **Small, focused migrations**: Make one logical change per migration
2. **Backward compatibility**: Ensure migrations can be applied to existing databases
3. **Data preservation**: Never drop tables or columns without data migration
4. **Test thoroughly**: Always test on a copy of production data
5. **Document changes**: Write clear migration messages describing what changed

## Troubleshooting

### Migration fails with "table already exists"

This usually means the migration was partially applied. Check the current version:

```bash
alembic current
```

If the migration shows as applied but failed partway through, you may need to manually fix the database state or mark the migration as complete:

```bash
alembic stamp <revision_id>
```

### Need to reset migrations (development only)

⚠️ **WARNING**: This will delete all data!

```bash
# Remove database
rm data/vocabulary.db

# Remove alembic_version from database (if it exists)
# Then recreate from scratch
alembic upgrade head
```

### Merge conflicts in migrations

If you have multiple migration branches, use:

```bash
alembic merge -m "merge migrations" head1 head2
```

## Initial Migration Details

The initial migration (`30fe5c606d36_initial_schema`) creates the following tables:

- `settings` - Application settings
- `learning_units` - Vocabulary entries (words/phrases/sentences)
- `learning_progress` - Progress tracking for each unit
- `learning_sessions` - Study session records
- `session_units` - Units within a session
- `audio_assets` - Audio pronunciation metadata

The migration is safe to run on existing databases - it checks for table existence before creating them.

## Related Files

- `alembic.ini` - Alembic configuration
- `alembic/env.py` - Alembic environment setup
- `alembic/versions/` - Migration files
- `app/database.py` - Database connection and Base
- `app/models/` - SQLAlchemy models
