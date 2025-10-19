"""
Site management API endpoints.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from app.api.deps import DBSession, CurrentUser, AdminUser
from app.models.site import Site
from app.models.category import SiteCategoryMapping
from app.schemas.site import (
    SiteCreate,
    SiteUpdate,
    SiteResponse,
    SiteDetailResponse,
    SiteListResponse,
    CategoryAssignment
)
from app.schemas.site_camera_layout import (
    AutoPopulateResponse,
    BulkAutoPopulateResponse
)
from app.services.site_camera_layout_service import (
    auto_populate_site_cameras,
    auto_populate_all_sites
)

router = APIRouter()


@router.get("", response_model=SiteListResponse)
async def list_sites(
    db: DBSession,
    current_user: CurrentUser,
    category_id: Optional[str] = Query(None),
    include_cameras: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100)
):
    """
    List all sites with optional filtering and pagination.

    - **category_id**: Filter sites by category UUID
    - **include_cameras**: Include camera count for each site
    - **page**: Page number (default: 1)
    - **per_page**: Items per page (default: 50, max: 100)
    """
    query = db.query(Site)

    # Apply category filter if provided
    if category_id:
        query = query.join(SiteCategoryMapping).filter(
            SiteCategoryMapping.category_id == category_id
        )

    # Get total count
    total = query.count()

    # Apply pagination
    offset = (page - 1) * per_page
    sites = query.offset(offset).limit(per_page).all()

    # Convert to response format
    sites_response = []
    for site in sites:
        site_data = SiteDetailResponse.model_validate(site)

        if include_cameras:
            site_data.camera_count = len(site.cameras) if hasattr(site, 'cameras') else 0

        # Add categories
        site_data.categories = [
            mapping.category for mapping in site.category_mappings
        ] if hasattr(site, 'category_mappings') else []

        sites_response.append(site_data)

    return SiteListResponse(
        sites=sites_response,
        total=total,
        page=page,
        per_page=per_page
    )


@router.post("", response_model=SiteResponse, status_code=status.HTTP_201_CREATED)
async def create_site(
    site_data: SiteCreate,
    db: DBSession,
    current_user: AdminUser
):
    """
    Create a new site.

    Requires admin or super_admin privileges.
    """
    # Generate site ID
    import uuid
    site_id = f"SITE_{uuid.uuid4().hex[:8].upper()}"

    # Create site
    new_site = Site(
        id=site_id,
        name=site_data.name,
        nvr_username=site_data.nvr_username,
        nvr_password=site_data.nvr_password,
        sureview_site=False,
        new=True
    )

    db.add(new_site)
    db.commit()
    db.refresh(new_site)

    return new_site


@router.get("/{site_id}", response_model=SiteDetailResponse)
async def get_site(
    site_id: str,
    db: DBSession,
    current_user: CurrentUser
):
    """
    Get single site with full details including cameras and categories.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site {site_id} not found"
        )

    return site


@router.put("/{site_id}", response_model=SiteResponse)
async def update_site(
    site_id: str,
    site_data: SiteUpdate,
    db: DBSession,
    current_user: AdminUser
):
    """
    Update site details.

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site {site_id} not found"
        )

    # Update fields if provided
    if site_data.name is not None:
        site.name = site_data.name
    if site_data.nvr_username is not None:
        site.nvr_username = site_data.nvr_username
    if site_data.nvr_password is not None:
        site.nvr_password = site_data.nvr_password

    db.commit()
    db.refresh(site)

    return site


@router.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(
    site_id: str,
    db: DBSession,
    current_user: AdminUser
):
    """
    Delete site and all associated data (cascades to cameras and layouts).

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site {site_id} not found"
        )

    db.delete(site)
    db.commit()

    return None


@router.put("/{site_id}/category")
async def assign_category_to_site(
    site_id: str,
    category_data: CategoryAssignment,
    db: DBSession,
    current_user: AdminUser
):
    """
    Assign category to site.

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site {site_id} not found"
        )

    # Check if mapping already exists
    existing_mapping = db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.site_id == site_id,
        SiteCategoryMapping.category_id == category_data.category_id
    ).first()

    if not existing_mapping:
        # Create new mapping
        mapping = SiteCategoryMapping(
            site_id=site_id,
            category_id=category_data.category_id
        )
        db.add(mapping)
        db.commit()

    return {"message": "Category assigned successfully"}


@router.post("/{site_id}/auto-populate-cameras", response_model=AutoPopulateResponse)
async def auto_populate_site_camera_layout(
    site_id: str,
    db: DBSession,
    current_user: AdminUser
):
    """
    Auto-populate camera layout for a single site.

    Creates or updates:
    - SiteCamerasLayoutConfig with optimal grid dimensions based on camera count
    - SiteCamerasLayout entries for each camera in row-major order

    Grid sizing logic:
    - 1 camera → 1×1 grid
    - 2 cameras → 1×2 grid
    - 3-4 cameras → 2×2 grid
    - 5-6 cameras → 2×3 grid
    - 7-9 cameras → 3×3 grid
    - 10-12 cameras → 3×4 grid
    - 13-16 cameras → 4×4 grid

    Maximum of 16 cameras can be assigned to site camera layout.

    Requires admin or super_admin privileges.
    """
    try:
        result = auto_populate_site_cameras(site_id, db)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-populate site cameras: {str(e)}"
        )


@router.post("/auto-populate-all-cameras", response_model=BulkAutoPopulateResponse)
async def auto_populate_all_site_cameras(
    db: DBSession,
    current_user: AdminUser
):
    """
    Auto-populate camera layouts for all sites that have cameras.

    Processes each site that has cameras and creates/updates:
    - SiteCamerasLayoutConfig with optimal grid dimensions
    - SiteCamerasLayout entries for each camera

    Sites without cameras are skipped.

    Returns summary including:
    - Total sites found
    - Sites successfully processed
    - Sites skipped (no cameras or errors)
    - Total cameras populated across all sites
    - Individual site results
    - Any errors encountered

    Requires admin or super_admin privileges.
    """
    try:
        result = auto_populate_all_sites(db)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-populate all site cameras: {str(e)}"
        )
