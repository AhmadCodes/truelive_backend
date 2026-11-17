#!/usr/bin/env python3
"""
Seed default system settings into the database.

Usage:
    python scripts/seed_settings.py

This script populates the system_settings table with default values
for all required settings categories. It uses the seed list of
app/services/settings_seeder.py, so the script and the API's startup seeding
always create the same keys (C10).
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal

# The one seed list and its seeding logic (the API seeds the same at startup).
from app.services.settings_seeder import DEFAULT_SETTINGS, seed_settings  # noqa: F401  (DEFAULT_SETTINGS: re-export)


def main():
    """Main entry point for seed script."""
    import argparse

    parser = argparse.ArgumentParser(description="Seed system settings")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Update existing settings with default values"
    )
    args = parser.parse_args()

    print("Seeding system settings...")

    db = SessionLocal()
    try:
        result = seed_settings(db, force=args.force)
        print(f"Created: {result['created']}")
        print(f"Skipped: {result['skipped']}")
        print(f"Updated: {result['updated']}")
        print(f"Total settings: {result['total']}")
        print("Done!")
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
