"""
View management API endpoints.
Provides bulk operations for fetching views across multiple screens.
"""

from fastapi import APIRouter, Query
from typing import List, Optional

from app.api.deps import DBSession, CurrentUser
from app.models.view import View
from app.schemas.screen import ViewResponse
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=List[ViewResponse])
async def list_all_views(
    current_user: CurrentUser,
    db: DBSession,
    screen_id: Optional[str] = Query(None, description="Filter views by a single screen ID"),
    screen_ids: Optional[str] = Query(None, description="Filter views by multiple screen IDs (comma-separated)")
):
    """
    List views with optional filtering by screen(s).

    This endpoint allows fetching views in bulk, reducing the number of API calls
    needed when working with multiple screens.

    **Query Parameters:**
    - **screen_id**: Filter by a single screen ID
    - **screen_ids**: Filter by multiple screen IDs (comma-separated string)
      - Example: `?screen_ids=screen1,screen2,screen3`

    **Examples:**
    - Get all views: `GET /api/v1/views`
    - Get views for one screen: `GET /api/v1/views?screen_id=abc-123`
    - Get views for multiple screens: `GET /api/v1/views?screen_ids=abc-123,def-456,ghi-789`

    **Performance Benefits:**
    - Fetching views for 4 screens: 1 API call instead of 4
    - Reduces network latency and server load

    All authenticated users can view views.

    Args:
        current_user: Current authenticated user
        db: Database session
        screen_id: Optional single screen ID filter
        screen_ids: Optional comma-separated screen IDs filter

    Returns:
        List of views matching the filter criteria (ordered by screen_id and view_number)

    Note:
        If both screen_id and screen_ids are provided, screen_ids takes precedence.
    """
    # Build query
    query = db.query(View)

    # Apply filters
    if screen_ids:
        # Parse comma-separated screen IDs
        screen_id_list = [s.strip() for s in screen_ids.split(',') if s.strip()]

        if screen_id_list:
            query = query.filter(View.screen_id.in_(screen_id_list))
            logger.info(f"Filtering views by {len(screen_id_list)} screen IDs")
    elif screen_id:
        query = query.filter(View.screen_id == screen_id)
        logger.info(f"Filtering views by screen ID: {screen_id}")

    # Order by screen_id and view_number for consistent results
    query = query.order_by(View.screen_id, View.view_number)

    # Execute query
    views = query.all()

    logger.info(f"Retrieved {len(views)} views")

    return views
