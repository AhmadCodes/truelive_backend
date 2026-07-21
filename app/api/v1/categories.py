"""
Device Category management API endpoints.
Includes category CRUD operations and device-category mapping management.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from typing import List, Optional
import uuid as uuid_lib

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.category import SiteCategory, SiteCategoryMapping
from app.models.device import Device
from app.schemas.category import (
    CategoryCreate,
    CategoryUpdate,
    CategoryResponse,
    CategoryDetailResponse,
    CategoryWithSiteCount,
    AssignCategoryRequest,
    UnassignCategoryRequest,
    BulkAssignRequest,
    CategoryMappingResponse,
    CategoryMappingDetailResponse,
    DeviceWithCategories,
    CategoryWithDevices
)


router = APIRouter()


# ==================== CATEGORY CRUD ENDPOINTS ====================

@router.post("", response_model=CategoryDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_data: CategoryCreate,
    current_user: AdminUser,
    db: DBSession
):
    """
    Create a new device category.

    Only admins and super admins can create categories.

    Args:
        category_data: Category creation data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Created category details

    Raises:
        HTTPException: If category name already exists
    """
    # Check if category name already exists
    existing_category = db.query(SiteCategory).filter(
        SiteCategory.name == category_data.name
    ).first()

    if existing_category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Category with name '{category_data.name}' already exists"
        )

    # Create new category
    new_category = SiteCategory(
        id=uuid_lib.uuid4(),
        name=category_data.name,
        color=category_data.color
    )

    db.add(new_category)
    db.commit()
    db.refresh(new_category)

    return new_category


@router.get("", response_model=List[CategoryWithSiteCount])
async def list_categories(
    current_user: CurrentUser,
    db: DBSession,
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=200, description="Number of records to return"),
    search: Optional[str] = Query(None, description="Search by category name")
):
    """
    List all categories with device counts.

    All authenticated users can view categories.

    Args:
        current_user: Current authenticated user
        db: Database session
        skip: Number of records to skip (pagination)
        limit: Number of records to return (pagination)
        search: Search by category name

    Returns:
        List of categories with device counts
    """
    query = db.query(SiteCategory)

    # Apply search filter
    if search:
        search_filter = f"%{search}%"
        query = query.filter(SiteCategory.name.ilike(search_filter))

    # Order by name
    query = query.order_by(SiteCategory.name)

    # Apply pagination
    categories = query.offset(skip).limit(limit).all()

    # Build response with device counts
    result = []
    for category in categories:
        device_count = db.query(func.count(SiteCategoryMapping.device_id)).filter(
            SiteCategoryMapping.category_id == category.id
        ).scalar() or 0

        result.append(
            CategoryWithSiteCount(
                id=category.id,
                name=category.name,
                color=category.color,
                device_count=device_count
            )
        )

    return result


@router.get("/count")
async def count_categories(
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get total count of categories.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        Total count of categories
    """
    total = db.query(func.count(SiteCategory.id)).scalar()
    return {"total": total}


# ==================== CATEGORY MAPPING ENDPOINTS ====================
# NOTE: All specific routes must come before /{category_id} to avoid path conflicts

@router.post("/assign", response_model=CategoryMappingResponse, status_code=status.HTTP_201_CREATED)
async def assign_category_to_device(
    request: AssignCategoryRequest,
    current_user: AdminUser,
    db: DBSession
):
    """
    Assign a category to a device.

    Only admins and super admins can assign categories.

    Args:
        request: Assignment request with device_id and category_id
        current_user: Current authenticated admin or super admin
        db: DBSession

    Returns:
        Created mapping details

    Raises:
        HTTPException: If device or category not found, or mapping already exists
    """
    # Verify device exists
    device = db.query(Device).filter(Device.id == request.device_id).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with ID '{request.device_id}' not found"
        )

    # Verify category exists
    category = db.query(SiteCategory).filter(SiteCategory.id == request.category_id).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category with ID '{request.category_id}' not found"
        )

    # Check if mapping already exists
    existing_mapping = db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.device_id == request.device_id,
        SiteCategoryMapping.category_id == request.category_id
    ).first()

    if existing_mapping:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Category '{category.name}' is already assigned to device '{device.name}'"
        )

    # Create mapping
    new_mapping = SiteCategoryMapping(
        device_id=request.device_id,
        category_id=request.category_id
    )

    db.add(new_mapping)
    db.commit()
    db.refresh(new_mapping)

    return new_mapping


@router.post("/assign-bulk", status_code=status.HTTP_200_OK)
async def bulk_assign_categories_to_device(
    request: BulkAssignRequest,
    current_user: AdminUser,
    db: DBSession
):
    """
    Bulk assign multiple categories to a device.

    Only admins and super admins can assign categories.
    This replaces all existing category assignments for the device.

    Args:
        request: Bulk assignment request with device_id and category_ids
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Summary of assignments

    Raises:
        HTTPException: If device not found or any category not found
    """
    # Verify device exists
    device = db.query(Device).filter(Device.id == request.device_id).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with ID '{request.device_id}' not found"
        )

    # Verify all categories exist
    categories = db.query(SiteCategory).filter(SiteCategory.id.in_(request.category_ids)).all()
    if len(categories) != len(request.category_ids):
        found_ids = {c.id for c in categories}
        missing_ids = set(request.category_ids) - found_ids
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Categories not found: {missing_ids}"
        )

    # Remove all existing mappings for this device
    db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.device_id == request.device_id
    ).delete(synchronize_session=False)

    # Create new mappings
    new_mappings = []
    for category_id in request.category_ids:
        new_mapping = SiteCategoryMapping(
            device_id=request.device_id,
            category_id=category_id
        )
        new_mappings.append(new_mapping)

    db.bulk_save_objects(new_mappings)
    db.commit()

    return {
        "success": True,
        "message": f"Assigned {len(request.category_ids)} categories to device '{device.name}'",
        "device_id": request.device_id,
        "category_count": len(request.category_ids)
    }


@router.post("/unassign", status_code=status.HTTP_204_NO_CONTENT)
async def unassign_category_from_device(
    request: UnassignCategoryRequest,
    current_user: AdminUser,
    db: DBSession
):
    """
    Unassign a category from a device.

    Only admins and super admins can unassign categories.

    Args:
        request: Unassignment request with device_id and category_id
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If mapping not found
    """
    mapping = db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.device_id == request.device_id,
        SiteCategoryMapping.category_id == request.category_id
    ).first()

    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category assignment not found for device '{request.device_id}' and category '{request.category_id}'"
        )

    db.delete(mapping)
    db.commit()

    return None


@router.get("/mappings", response_model=List[CategoryMappingDetailResponse])
async def list_all_mappings(
    current_user: CurrentUser,
    db: DBSession,
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=500, description="Number of records to return")
):
    """
    List all category-device mappings with details.

    All authenticated users can view mappings.

    Args:
        current_user: Current authenticated user
        db: Database session
        skip: Number of records to skip (pagination)
        limit: Number of records to return (pagination)

    Returns:
        List of all mappings with device and category details
    """
    mappings = db.query(SiteCategoryMapping).options(
        joinedload(SiteCategoryMapping.device),
        joinedload(SiteCategoryMapping.category)
    ).offset(skip).limit(limit).all()

    result = []
    for mapping in mappings:
        result.append(
            CategoryMappingDetailResponse(
                device_id=mapping.device_id,
                category_id=mapping.category_id,
                assigned_at=mapping.assigned_at,
                device_name=mapping.device.name if mapping.device else None,
                category_name=mapping.category.name if mapping.category else None,
                category_color=mapping.category.color if mapping.category else None
            )
        )

    return result


@router.get("/device/{device_id}/categories", response_model=List[CategoryResponse])
async def get_categories_for_device(
    device_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get all categories assigned to a specific device.

    All authenticated users can view category assignments.

    Args:
        device_id: Device ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of categories assigned to the device

    Raises:
        HTTPException: If device not found
    """
    # Verify device exists
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with ID '{device_id}' not found"
        )

    # Get categories for the device
    categories = db.query(SiteCategory).join(
        SiteCategoryMapping,
        SiteCategory.id == SiteCategoryMapping.category_id
    ).filter(
        SiteCategoryMapping.device_id == device_id
    ).order_by(SiteCategory.name).all()

    return categories


@router.get("/category/{category_id}/devices")
async def get_devices_for_category(
    category_id: uuid_lib.UUID,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get all devices assigned to a specific category.

    All authenticated users can view category assignments.

    Args:
        category_id: Category UUID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of devices with this category

    Raises:
        HTTPException: If category not found
    """
    # Verify category exists
    category = db.query(SiteCategory).filter(SiteCategory.id == category_id).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category with ID '{category_id}' not found"
        )

    # Get devices for the category
    devices = db.query(Device).join(
        SiteCategoryMapping,
        Device.id == SiteCategoryMapping.device_id
    ).filter(
        SiteCategoryMapping.category_id == category_id
    ).order_by(Device.name).all()

    # Build response
    device_list = [
        {
            "device_id": device.id,
            "name": device.name,
            "site_id": device.site_id,
            "customer_id": device.site.customer_id if device.site else None
        }
        for device in devices
    ]

    return {
        "category_id": category.id,
        "category_name": category.name,
        "category_color": category.color,
        "device_count": len(device_list),
        "devices": device_list
    }


@router.delete("/device/{device_id}/categories", status_code=status.HTTP_204_NO_CONTENT)
async def remove_all_categories_from_device(
    device_id: str,
    current_user: AdminUser,
    db: DBSession
):
    """
    Remove all category assignments from a device.

    Only admins and super admins can remove category assignments.

    Args:
        device_id: Device ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If device not found
    """
    # Verify device exists
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with ID '{device_id}' not found"
        )

    # Delete all mappings for this device
    deleted_count = db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.device_id == device_id
    ).delete(synchronize_session=False)

    db.commit()

    return None


# ==================== PARAMETRIZED ROUTES (must be last) ====================

@router.get("/{category_id}", response_model=CategoryDetailResponse)
async def get_category(
    category_id: uuid_lib.UUID,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get single category by ID.

    All authenticated users can view categories.

    Args:
        category_id: Category UUID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Category details

    Raises:
        HTTPException: If category not found
    """
    category = db.query(SiteCategory).filter(SiteCategory.id == category_id).first()

    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category with ID '{category_id}' not found"
        )

    return category


@router.put("/{category_id}", response_model=CategoryDetailResponse)
async def update_category(
    category_id: uuid_lib.UUID,
    category_data: CategoryUpdate,
    current_user: AdminUser,
    db: DBSession
):
    """
    Update category details.

    Only admins and super admins can update categories.

    Args:
        category_id: Category UUID
        category_data: Category update data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated category details

    Raises:
        HTTPException: If category not found or name already exists
    """
    category = db.query(SiteCategory).filter(SiteCategory.id == category_id).first()

    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category with ID '{category_id}' not found"
        )

    # Update name if provided
    if category_data.name is not None:
        # Check if new name already exists for another category
        existing_category = db.query(SiteCategory).filter(
            SiteCategory.name == category_data.name,
            SiteCategory.id != category_id
        ).first()

        if existing_category:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Category with name '{category_data.name}' already exists"
            )

        category.name = category_data.name

    # Update color if provided
    if category_data.color is not None:
        category.color = category_data.color

    db.commit()
    db.refresh(category)

    return category


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid_lib.UUID,
    current_user: AdminUser,
    db: DBSession,
    force: bool = Query(False, description="Force delete even if devices are using this category")
):
    """
    Delete a category.

    Only admins and super admins can delete categories.
    By default, categories with assigned devices cannot be deleted (use force=true to override).

    Args:
        category_id: Category UUID
        current_user: Current authenticated admin or super admin
        db: Database session
        force: Force delete even if devices are using this category

    Raises:
        HTTPException: If category not found or has assigned devices (without force)
    """
    category = db.query(SiteCategory).filter(SiteCategory.id == category_id).first()

    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category with ID '{category_id}' not found"
        )

    # Check if category has assigned devices
    if not force:
        device_count = db.query(func.count(SiteCategoryMapping.device_id)).filter(
            SiteCategoryMapping.category_id == category_id
        ).scalar() or 0

        if device_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete category '{category.name}' because it has {device_count} device(s) assigned. Use force=true to delete anyway."
            )

    db.delete(category)
    db.commit()

    return None
