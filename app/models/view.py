"""
View model for managing camera view layouts on screens.
"""

from sqlalchemy import Column, String, Integer, CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, ActorStampMixin


class View(BaseModel, ActorStampMixin):
    """
    View model representing camera view layouts on screens.

    Each screen can have multiple views that rotate based on switching_interval.
    Each view defines a grid layout for displaying cameras.

    Attributes:
        id: Unique identifier for the view
        screen_id: Reference to the screen this view belongs to
        name: Display name of the view
        layout_rows: Number of rows in the view layout grid (1-10)
        layout_columns: Number of columns in the view layout grid (1-10)
        view_number: Sequential number of this view on the screen
    """

    __tablename__ = "views"

    # Primary key
    id = Column(
        String(255),
        primary_key=True,
        comment="Unique identifier for the view"
    )

    # Foreign key to Screen
    screen_id = Column(
        String(100),
        ForeignKey('screens.id', ondelete='CASCADE'),
        nullable=False,
        comment="ID of the screen this view belongs to"
    )

    # Basic information
    name = Column(
        String(50),
        nullable=False,
        comment="Display name of the view"
    )

    # Layout configuration
    layout_rows = Column(
        Integer,
        nullable=False,
        comment="Number of rows in the view layout grid (1-10)"
    )

    layout_columns = Column(
        Integer,
        nullable=False,
        comment="Number of columns in the view layout grid (1-10)"
    )

    # View ordering
    view_number = Column(
        Integer,
        nullable=False,
        comment="Sequential number of this view on the screen"
    )

    # Constraints
    __table_args__ = (
        CheckConstraint(
            "layout_rows BETWEEN 1 AND 10",
            name='check_view_layout_rows'
        ),
        CheckConstraint(
            "layout_columns BETWEEN 1 AND 10",
            name='check_view_layout_columns'
        ),
        # Unique constraint on screen_id and view_number
        UniqueConstraint(
            'screen_id',
            'view_number',
            name='uq_screen_view_number'
        ),
        # Indexes
        Index('idx_views_screen_id', 'screen_id'),
        Index('idx_views_view_number', 'view_number'),
    )

    # Relationships
    screen = relationship(
        "Screen",
        back_populates="views",
        doc="Screen this view belongs to"
    )

    screen_mappings = relationship(
        "ScreenMapping",
        back_populates="view",
        cascade="all, delete-orphan",
        doc="Screen mappings for this view"
    )

    def __repr__(self):
        """String representation of View."""
        return f"<View(id='{self.id}', name='{self.name}', screen_id='{self.screen_id}', view_number={self.view_number})>"

    def to_dict(self):
        """
        Convert View instance to dictionary.

        Returns:
            Dictionary representation of the view
        """
        return {
            'id': self.id,
            'screen_id': self.screen_id,
            'name': self.name,
            'layout_rows': self.layout_rows,
            'layout_columns': self.layout_columns,
            'view_number': self.view_number,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }

    @property
    def total_slots(self):
        """
        Calculate total number of slots in the view layout.

        Returns:
            Total number of slots (layout_rows * layout_columns)
        """
        return self.layout_rows * self.layout_columns

    @property
    def max_cameras(self):
        """
        Get maximum number of cameras that can be displayed in this view.

        Returns:
            Maximum number of cameras (same as total_slots)
        """
        return self.total_slots
