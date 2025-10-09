"""
Pydantic schemas for SureView API endpoints.
"""

from pydantic import BaseModel, RootModel, Field
from typing import List, Optional


# Site-related schemas
class SiteDetail(BaseModel):
    """Individual site details for get_sites response."""

    address: Optional[str] = None
    telephone: Optional[str] = None
    telephone2: Optional[str] = None
    telephonePolice: Optional[str] = None
    telephoneFire: Optional[str] = None
    notes: Optional[str] = None
    latLong: Optional[str] = None
    site_id: str
    name: str
    camera_count: int = 0

    class Config:
        from_attributes = True


class GetSitesRequest(BaseModel):
    """Request schema for get_sites endpoint."""

    customer_id: str = Field(..., description="Customer ID to filter sites")
    site_ids: Optional[List[str]] = Field(None, description="Optional list of site IDs to filter")


class GetSitesResponse(BaseModel):
    """Response schema for get_sites endpoint."""

    customer_id: str
    sites: List[SiteDetail]


class CustomerSiteSummary(BaseModel):
    """Summary of a single site for get_all_sites response."""

    customer_id: str
    site_id: str
    name: str
    camera_count: int = 0


class CustomerSitesGroup(BaseModel):
    """Group of sites for a customer in get_all_sites response."""

    customer_id: str
    customer_sites: List[CustomerSiteSummary]


# Use alias for backward compatibility - the response is just a list
GetAllSitesResponse = List[CustomerSitesGroup]


# Camera-related schemas
class CameraDetail(BaseModel):
    """Individual camera details."""

    camera_id: str
    camera_name: str
    rtsp_url: str

    class Config:
        from_attributes = True


class GetCamerasRequest(BaseModel):
    """Request schema for get_cameras endpoint."""

    site_id: str = Field(..., description="Site ID to get cameras for")


# Use alias for backward compatibility - the response is just a list
GetCamerasResponse = List[CameraDetail]


# SureView API integration schemas (for internal use)
class SureViewServer(BaseModel):
    """SureView server data from GetServerList API."""

    serverID: int
    title: str
    groupID: int
    host: str
    port: int
    username: str
    password: str
    extraValue: Optional[str] = None


class SureViewGroup(BaseModel):
    """SureView group data from GetGroup API."""

    groupID: int
    title: str
    address: Optional[str] = None
    telephone: Optional[str] = None
    telephone2: Optional[str] = None
    telephonePolice: Optional[str] = None
    telephoneFire: Optional[str] = None
    notes: Optional[str] = None
    latLong: Optional[str] = None
    referenceId: Optional[str] = None  # This is the customer_id
