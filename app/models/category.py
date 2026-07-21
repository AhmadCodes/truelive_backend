"""
Site category models for organizing sites.
"""

from sqlalchemy import (
    Column, String, BigInteger, UUID, DateTime,
    ForeignKey, Index, PrimaryKeyConstraint, text
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class SiteCategory(BaseModel):
    """
    Site category model for organizing sites into groups.

    Attributes:
        id: Primary key (UUID)
        name: Unique category name
        color: Color in 0xFFRRGGBBAA format (stored as BIGINT)
    """
    __tablename__ = "site_categories"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="Unique category identifier"
    )
    name = Column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique category name"
    )
    color = Column(
        BigInteger,
        nullable=False,
        comment="Color in 0xFFRRGGBBAA format"
    )

    # Relationships
    site_mappings = relationship(
        "SiteCategoryMapping",
        back_populates="category",
        cascade="all, delete-orphan"
    )

    # Table constraints
    __table_args__ = (
        Index("idx_categories_name", "name"),
    )

    def __repr__(self):
        return (
            f"<SiteCategory(id={self.id}, name='{self.name}', color={self.color})>"
        )


class SiteCategoryMapping(BaseModel):
    """
    Many-to-many mapping between sites and categories.

    Attributes:
        site_id: Reference to site
        category_id: Reference to category
        assigned_at: Timestamp when the mapping was created
    """
    __tablename__ = "site_category_mappings"

    site_id = Column(
        String(255),
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
        comment="Site identifier"
    )
    category_id = Column(
        UUID(as_uuid=True),
        ForeignKey("site_categories.id", ondelete="RESTRICT"),
        nullable=False,
        primary_key=True,
        comment="Category identifier"
    )
    assigned_at = Column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
        comment="Timestamp when the mapping was created"
    )

    # Relationships
    site = relationship(
        "Site",
        back_populates="category_mappings"
    )
    category = relationship(
        "SiteCategory",
        back_populates="site_mappings"
    )

    # Table constraints
    __table_args__ = (
        PrimaryKeyConstraint("site_id", "category_id"),
        Index("idx_mappings_category", "category_id"),
        Index("idx_mappings_site", "site_id"),
    )

    def __repr__(self):
        return (
            f"<SiteCategoryMapping(site_id='{self.site_id}', "
            f"category_id={self.category_id}, assigned_at={self.assigned_at})>"
        )
