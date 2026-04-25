"""
Database initialization script.
Creates initial super admin user and default category.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session
from app.database import SessionLocal, engine, Base
from app.models.user import User
from app.models.category import SiteCategory
from app.core.security import get_password_hash
import uuid


def init_database():
    """Initialize database with tables and seed data."""

    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    print("✓ Tables created")

    db = SessionLocal()

    try:
        # Check if super admin already exists
        existing_admin = db.query(User).filter(User.role == 'super_admin').first()

        if existing_admin:
            print(f"✓ Super admin already exists: {existing_admin.username}")
        else:
            # Create default super admin
            print("Creating super admin user...")
            admin_user = User(
                user_id=uuid.uuid4(),
                username='admin',
                email='admin@trueliveportal.com',
                password_hash=get_password_hash('admin123'),  # CHANGE THIS IN PRODUCTION!
                role='super_admin',
                is_active=True
            )
            db.add(admin_user)
            db.commit()
            print("✓ Super admin created")
            print("  Username: admin")
            print("  Password: admin123")
            print("  ⚠️  IMPORTANT: Change the password immediately!")

        # Check if default category exists
        existing_category = db.query(SiteCategory).filter(SiteCategory.name == 'Default').first()

        if existing_category:
            print(f"✓ Default category already exists")
        else:
            # Create default category
            print("Creating default category...")
            default_category = SiteCategory(
                id=uuid.uuid4(),
                name='Default',
                color=4294967295  # 0xFFFFFFFF (white)
            )
            db.add(default_category)
            db.commit()
            print("✓ Default category created")

        print("\n✅ Database initialization complete!")
        print("\nNext steps:")
        print("1. Start the FastAPI server: uvicorn app.main:app --reload")
        print("2. Login at http://localhost:8000/api/v1/docs")
        print("3. Change the admin password")

    except Exception as e:
        print(f"\n❌ Error during initialization: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    init_database()
