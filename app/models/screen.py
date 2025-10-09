"""
Screen model for managing display screens connected to PCs.
"""

from sqlalchemy import Column, String, Integer, CheckConstraint, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Screen(BaseModel):
    """
    Screen model representing physical displays connected to PCs.

    Attributes:
        id: Unique identifier for the screen
        pc_id: Reference to the PC this screen is connected to
        name: Display name of the screen
        rows: Number of rows in the screen grid (1-4)
        columns: Number of columns in the screen grid (1-4)
        switching_interval: Interval in seconds for view switching
    """

    __tablename__ = "screens"

    # Primary key
    id = Column(
        String(100),
        primary_key=True,
        comment="Unique identifier for the screen"
    )

    # Foreign key to PC
    pc_id = Column(
        String(50),
        ForeignKey('pcs.id', ondelete='CASCADE'),
        nullable=False,
        comment="ID of the PC this screen is connected to"
    )

    # Basic information
    name = Column(
        String(100),
        nullable=False,
        comment="Display name of the screen"
    )

    # Grid configuration
    rows = Column(
        Integer,
        nullable=False,
        comment="Number of rows in the screen grid (1-4)"
    )

    columns = Column(
        Integer,
        nullable=False,
        comment="Number of columns in the screen grid (1-4)"
    )

    # Switching configuration
    switching_interval = Column(
        Integer,
        nullable=False,
        comment="Interval in seconds for view switching (minimum 1)"
    )

    # Constraints
    __table_args__ = (
        CheckConstraint(
            "rows BETWEEN 1 AND 4",
            name='check_screen_rows'
        ),
        CheckConstraint(
            "columns BETWEEN 1 AND 4",
            name='check_screen_columns'
        ),
        CheckConstraint(
            "switching_interval >= 1",
            name='check_switching_interval'
        ),
        # Indexes
        Index('idx_screens_pc_id', 'pc_id'),
    )

    # Relationships
    pc = relationship(
        "PC",
        back_populates="screens",
        doc="PC this screen is connected to"
    )

    views = relationship(
        "View",
        back_populates="screen",
        cascade="all, delete-orphan",
        doc="Views configured for this screen"
    )

    screen_mappings = relationship(
        "ScreenMapping",
        back_populates="screen",
        cascade="all, delete-orphan",
        doc="Screen mappings for this screen"
    )

    def __repr__(self):
        """String representation of Screen."""
        return f"<Screen(id='{self.id}', name='{self.name}', pc_id='{self.pc_id}')>"

    def to_dict(self):
        """
        Convert Screen instance to dictionary.

        Returns:
            Dictionary representation of the screen
        """
        return {
            'id': self.id,
            'pc_id': self.pc_id,
            'name': self.name,
            'rows': self.rows,
            'columns': self.columns,
            'switching_interval': self.switching_interval,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }

    @property
    def total_slots(self):
        """
        Calculate total number of slots in the screen grid.

        Returns:
            Total number of slots (rows * columns)
        """
        return self.rows * self.columns
