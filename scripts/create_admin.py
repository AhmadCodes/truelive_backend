#!/usr/bin/env python3
"""Create admin user with correct password hash."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid
from app.database import SessionLocal
from app.models.user import User
from app.core.security import get_password_hash

def create_admin():
    db = SessionLocal()
    try:
        # Delete existing admin if exists
        db.query(User).filter(User.username == 'admin').delete()
        db.commit()

        # Create fresh admin user
        admin_user = User(
            user_id=uuid.uuid4(),
            username='admin',
            full_name='TrueLive Admin',
            email='admin@trueliveportal.com',
            password_hash=get_password_hash('admin@USVG1'),
            role='super_admin',
            is_active=True
        )
        db.add(admin_user)
        db.commit()
        print('✓ Admin user created successfully')
        print('  Username: admin')
        print('  Password: admin@USVG1')
        print('  Name: TrueLive Admin')
        print('  Role: super_admin')
    except Exception as e:
        print(f'❌ Error: {e}')
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    create_admin()
