"""
Database connection and session management.
"""

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool
from typing import Generator
from app.core.config import settings


# Create SQLAlchemy engine
engine = create_engine(
    settings.database_url_sync,
    poolclass=QueuePool,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,  # Enable connection health checks
    echo=settings.DATABASE_ECHO,
)


# Create SessionLocal class
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


# Create Base class for models
Base = declarative_base()


# Dependency to get database session
def get_db() -> Generator:
    """
    Dependency function to get database session.
    Automatically closes session after request.

    Yields:
        Database session
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Event listener for PostgreSQL-specific optimizations
@event.listens_for(engine, "connect")
def set_postgres_pragmas(dbapi_conn, connection_record):
    """Set PostgreSQL connection parameters for optimization."""
    cursor = dbapi_conn.cursor()
    # Set statement timeout (30 seconds)
    cursor.execute("SET statement_timeout = '30000'")
    # Set lock timeout (10 seconds)
    cursor.execute("SET lock_timeout = '10000'")
    cursor.close()


def init_db():
    """
    Initialize database tables.
    This should be called only for testing or initial setup.
    Use Alembic migrations for production.
    """
    Base.metadata.create_all(bind=engine)


def check_db_health() -> bool:
    """
    Check database connection health.

    Returns:
        True if database is healthy, False otherwise
    """
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        return True
    except Exception:
        return False
