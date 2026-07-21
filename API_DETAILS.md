# API Documentation

Complete API reference for the TrueLive Portal surveillance camera management system.

**Base URL:** `http://localhost:8000/api/v1`

**Authentication:** Most endpoints require JWT Bearer token authentication. Include the token in the `Authorization` header:
```
Authorization: Bearer <access_token>
```

**Authentication Levels:**
- **CurrentUser** - Any authenticated user (user, admin, super_admin)
- **AdminUser** - Admin or super_admin only
- **SuperAdminUser** - Super_admin only

---

## Table of Contents

1. [Authentication](#authentication)
2. [Users](#users)
3. [Sites](#sites)
4. [Cameras](#cameras)
5. [PCs](#pcs)
6. [Screens, Views & Mappings](#screens-views--mappings)
7. [Categories](#categories)
8. [Configurations](#configurations)
9. [Snapshots](#snapshots)

---

## Authentication

### POST /auth/login

Login and obtain access and refresh tokens.

**Authentication:** None (public)

**Request Body:**
```json
{
  "username": "string",
  "password": "string",
  "remember_me": boolean  // Optional, default: false
}
```

**Response:** `200 OK`
```json
{
  "access_token": "string",
  "refresh_token": "string",
  "token_type": "bearer",
  "expires_in": integer  // Seconds until token expires
}
```

**Notes:**
- If `remember_me` is true, access token expires in 7 days instead of the default
- Last login timestamp is updated

---

### POST /auth/refresh

Refresh access token using refresh token.

**Authentication:** None (public, but requires valid refresh token)

**Request Body:**
```json
{
  "refresh_token": "string"
}
```

**Response:** `200 OK`
```json
{
  "access_token": "string",
  "refresh_token": "string",  // Same as provided
  "token_type": "bearer",
  "expires_in": integer
}
```

**Errors:**
- `401 Unauthorized` - Invalid or expired refresh token

---

### POST /auth/logout

Logout endpoint (for audit logging).

**Authentication:** CurrentUser

**Response:** `200 OK`
```json
{
  "message": "Successfully logged out"
}
```

**Notes:**
- In a stateless JWT system, actual logout happens client-side by discarding tokens
- This endpoint is primarily for audit logging

---

### GET /auth/me

Get current user information.

**Authentication:** CurrentUser

**Response:** `200 OK`
```json
{
  "user_id": "uuid",
  "username": "string",
  "email": "string",
  "role": "string",  // "user", "admin", or "super_admin"
  "is_active": boolean,
  "created_at": "datetime",
  "updated_at": "datetime",
  "last_login": "datetime"
}
```

---

### PATCH /auth/me/email

Update current user's email address.

**Authentication:** CurrentUser

**Request Body:**
```json
{
  "email": "string"
}
```

**Response:** `200 OK` - Returns updated user object

**Errors:**
- `400 Bad Request` - Email already registered

---

### POST /auth/me/password

Change current user's password.

**Authentication:** CurrentUser

**Request Body:**
```json
{
  "current_password": "string",
  "new_password": "string"
}
```

**Response:** `200 OK`
```json
{
  "message": "Password changed successfully"
}
```

**Errors:**
- `400 Bad Request` - Current password incorrect or new password too weak

---

## Users

### POST /users

Create a new user.

**Authentication:** SuperAdminUser

**Request Body:**
```json
{
  "username": "string",
  "email": "string",
  "password": "string",
  "role": "string",  // "user", "admin", or "super_admin"
  "is_active": boolean  // Optional, default: true
}
```

**Response:** `201 Created`
```json
{
  "user_id": "uuid",
  "username": "string",
  "email": "string",
  "role": "string",
  "is_active": boolean,
  "created_by": "uuid",
  "created_at": "datetime",
  "updated_at": "datetime",
  "last_login": "datetime"
}
```

**Errors:**
- `400 Bad Request` - Username or email already exists, or password too weak

---

### GET /users

List all users with optional filtering.

**Authentication:** AdminUser

**Query Parameters:**
- `skip` (integer, default: 0) - Number of records to skip
- `limit` (integer, default: 50, max: 100) - Number of records to return
- `role` (string) - Filter by role ("user", "admin", "super_admin")
- `is_active` (boolean) - Filter by active status
- `search` (string) - Search by username or email (case-insensitive)

**Response:** `200 OK` - Array of user objects

---

### GET /users/count

Get total count of users.

**Authentication:** AdminUser

**Query Parameters:**
- `role` (string) - Filter by role
- `is_active` (boolean) - Filter by active status

**Response:** `200 OK`
```json
{
  "total": integer
}
```

---

### GET /users/{user_id}

Get single user by ID.

**Authentication:** AdminUser

**Path Parameters:**
- `user_id` (UUID) - User UUID

**Response:** `200 OK` - User object

**Errors:**
- `404 Not Found` - User not found

---

### PUT /users/{user_id}

Update user details.

**Authentication:** SuperAdminUser

**Path Parameters:**
- `user_id` (UUID) - User UUID

**Request Body:** (all fields optional)
```json
{
  "email": "string",
  "role": "string",
  "is_active": boolean
}
```

**Response:** `200 OK` - Updated user object

**Errors:**
- `400 Bad Request` - Cannot change own role or deactivate own account, or email already exists
- `404 Not Found` - User not found

---

### DELETE /users/{user_id}

Delete a user.

**Authentication:** SuperAdminUser

**Path Parameters:**
- `user_id` (UUID) - User UUID

**Response:** `204 No Content`

**Errors:**
- `400 Bad Request` - Cannot delete own account
- `404 Not Found` - User not found

---

### POST /users/{user_id}/reset-password

Reset a user's password (admin action).

**Authentication:** SuperAdminUser

**Path Parameters:**
- `user_id` (UUID) - User UUID

**Request Body:**
```json
{
  "new_password": "string"
}
```

**Response:** `200 OK`
```json
{
  "message": "Password reset successfully for user <username>"
}
```

**Errors:**
- `400 Bad Request` - Password too weak
- `404 Not Found` - User not found

---

### PATCH /users/{user_id}/activate

Activate a user account.

**Authentication:** SuperAdminUser

**Path Parameters:**
- `user_id` (UUID) - User UUID

**Response:** `200 OK`
```json
{
  "message": "User <username> activated successfully",
  "user": {...}
}
```

---

### PATCH /users/{user_id}/deactivate

Deactivate a user account.

**Authentication:** SuperAdminUser

**Path Parameters:**
- `user_id` (UUID) - User UUID

**Response:** `200 OK`
```json
{
  "message": "User <username> deactivated successfully",
  "user": {...}
}
```

**Errors:**
- `400 Bad Request` - Cannot deactivate own account

---

## Sites

### GET /sites

List all sites with optional filtering and pagination.

**Authentication:** CurrentUser

**Query Parameters:**
- `category_id` (string) - Filter by category UUID
- `include_cameras` (boolean, default: false) - Include camera count
- `page` (integer, default: 1, min: 1) - Page number
- `per_page` (integer, default: 50, min: 1, max: 100) - Items per page

**Response:** `200 OK`
```json
{
  "sites": [
    {
      "id": "string",
      "name": "string",
      "customer_id": "string",
      "nvr_username": "string",
      "nvr_password": "string",
      "address": "string",
      "telephone": "string",
      "telephone2": "string",
      "telephone_police": "string",
      "telephone_fire": "string",
      "notes": "string",
      "lat_long": "string",
      "new": boolean,
      "camera_count": integer,  // If include_cameras=true
      "categories": [],  // Array of category objects
      "created_at": "datetime",
      "updated_at": "datetime"
    }
  ],
  "total": integer,
  "page": integer,
  "per_page": integer
}
```

---

### POST /sites

Create a new site.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "name": "string",
  "nvr_username": "string",
  "nvr_password": "string",
  "use_tcp": boolean  // Optional, default false — site-wide RTSP TCP default (overridable per camera)
}
```

**Response:** `201 Created`
```json
{
  "id": "string",  // Auto-generated: SITE_<8_hex_chars>
  "name": "string",
  "nvr_username": "string",
  "nvr_password": "string",
  "new": true,
  "use_tcp": boolean,
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

---

### GET /sites/{site_id}

Get single site with full details.

**Authentication:** CurrentUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK` - Site object with cameras and categories

**Errors:**
- `404 Not Found` - Site not found

---

### PUT /sites/{site_id}

Update site details.

**Authentication:** AdminUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Request Body:** (all fields optional)
```json
{
  "name": "string",
  "nvr_username": "string",
  "nvr_password": "string",
  "use_tcp": boolean  // Site-wide RTSP TCP default; cameras with use_tcp=null inherit this
}
```

**Response:** `200 OK` - Updated site object

**Errors:**
- `404 Not Found` - Site not found

---

### DELETE /sites/{site_id}

Delete site and all associated data (cascades to cameras and layouts).

**Authentication:** AdminUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `204 No Content`

**Errors:**
- `404 Not Found` - Site not found

---

### PUT /sites/{site_id}/category

Assign category to site.

**Authentication:** AdminUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Request Body:**
```json
{
  "category_id": "uuid"
}
```

**Response:** `200 OK`
```json
{
  "message": "Category assigned successfully"
}
```

**Errors:**
- `404 Not Found` - Site not found

---

### POST /sites/{site_id}/auto-populate-cameras

Auto-populate camera layout for a single site.

**Authentication:** AdminUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK`
```json
{
  "success": true,
  "site_id": "string",
  "site_name": "string",
  "rows": integer,
  "columns": integer,
  "cameras_assigned": integer,
  "total_slots": integer,
  "message": "string"
}
```

**Grid Sizing Logic:**
- 1 camera → 1×1 grid
- 2 cameras → 1×2 grid
- 3-4 cameras → 2×2 grid
- 5-6 cameras → 2×3 grid
- 7-9 cameras → 3×3 grid
- 10-12 cameras → 3×4 grid
- 13-16 cameras → 4×4 grid (max)

**Errors:**
- `404 Not Found` - Site not found

---

### POST /sites/auto-populate-all-cameras

Auto-populate camera layouts for all sites.

**Authentication:** AdminUser

**Response:** `200 OK`
```json
{
  "success": true,
  "total_sites_found": integer,
  "sites_processed": integer,
  "sites_skipped": integer,
  "total_cameras_populated": integer,
  "results": [
    {
      "site_id": "string",
      "site_name": "string",
      "success": boolean,
      "cameras_assigned": integer,
      "error": "string"  // If failed
    }
  ],
  "errors": []  // Array of error messages
}
```

---

## Cameras

### POST /cameras

Create a new camera.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "id": "string",
  "site_id": "string",
  "name": "string",
  "rtsp_url": "string",
  "main_stream_url": "string",  // Optional
  "new": boolean,  // Default: true
  "use_tcp": boolean | null  // Default: null — null inherits site.use_tcp; true/false overrides
}
```

**Response:** `201 Created`
```json
{
  "id": "string",
  "site_id": "string",
  "site_name": "string",
  "name": "string",
  "rtsp_url": "string",
  "main_stream_url": "string",
  "new": boolean,
  "use_tcp": boolean | null,  // null = inherit site.use_tcp
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

**Errors:**
- `400 Bad Request` - Camera ID already exists
- `404 Not Found` - Site not found

---

### GET /cameras

List all cameras with optional filtering.

**Authentication:** CurrentUser

**Query Parameters:**
- `skip` (integer, default: 0) - Records to skip
- `limit` (integer, default: 50, max: 500) - Records to return
- `site_id` (string) - Filter by site ID
- `new` (boolean) - Filter by new flag
- `search` (string) - Search by camera name or ID

**Response:** `200 OK` - Array of camera objects with `site_name` included

---

### GET /cameras/count

Get total count of cameras.

**Authentication:** CurrentUser

**Query Parameters:**
- `site_id` (string) - Filter by site ID
- `new` (boolean) - Filter by new flag

**Response:** `200 OK`
```json
{
  "total": integer
}
```

---

### GET /cameras/{camera_id}

Get single camera by ID.

**Authentication:** CurrentUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Response:** `200 OK` - Camera object with `site_name`

**Errors:**
- `404 Not Found` - Camera not found

---

### PUT /cameras/{camera_id}

Update camera details.

**Authentication:** AdminUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Request Body:** (all fields optional)
```json
{
  "site_id": "string",
  "name": "string",
  "rtsp_url": "string",
  "main_stream_url": "string",
  "new": boolean,
  "use_tcp": boolean | null  // Omit: no change. true/false: override. null: clear override (inherit site)
}
```

**Response:** `200 OK` - Updated camera object with `site_name`

**Errors:**
- `404 Not Found` - Camera or new site not found

---

### DELETE /cameras/{camera_id}

Delete a camera.

**Authentication:** AdminUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Response:** `204 No Content`

**Errors:**
- `404 Not Found` - Camera not found

---

### PATCH /cameras/{camera_id}/mark-as-seen

Mark camera as no longer new (set new=False).

**Authentication:** AdminUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Response:** `200 OK`
```json
{
  "message": "Camera '<name>' marked as seen",
  "camera": {...}
}
```

---

### PATCH /cameras/{camera_id}/toggle-new

Toggle the new flag for a camera.

**Authentication:** AdminUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Response:** `200 OK`
```json
{
  "message": "Camera '<name>' new flag set to <boolean>",
  "camera": {...}
}
```

---

### GET /cameras/site/{site_id}

Get all cameras for a specific site.

**Authentication:** CurrentUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK` - Array of camera objects with `site_name`

**Errors:**
- `404 Not Found` - Site not found

---

## PCs

### POST /pcs

Create a new PC.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "id": "string",
  "name": "string",
  "ip_address": "string",
  "gpu_type": "string",  // Optional
  "role": "string",  // "controller" or "manager"
  "manager_id": "string"  // Optional, for controller PCs only
}
```

**Response:** `201 Created`
```json
{
  "id": "string",
  "name": "string",
  "ip_address": "string",
  "gpu_type": "string",
  "role": "string",
  "manager_id": "string",
  "last_connected": "datetime",
  "last_applied": "datetime"
}
```

**Errors:**
- `400 Bad Request` - Manager PCs cannot have manager_id, or referenced PC is not a manager
- `404 Not Found` - Manager PC not found
- `409 Conflict` - PC ID already exists

---

### GET /pcs

List all PCs with optional filters.

**Authentication:** CurrentUser

**Query Parameters:**
- `role` (string) - Filter by role ("controller" or "manager")
- `manager_id` (string) - Filter by manager PC ID
- `search` (string) - Search by name or IP address

**Response:** `200 OK` - Array of PC objects with `screen_count`

---

### GET /pcs/count

Get count of PCs.

**Authentication:** CurrentUser

**Query Parameters:**
- `role` (string) - Filter by role

**Response:** `200 OK`
```json
{
  "total": integer,
  "controllers": integer,
  "managers": integer
}
```

---

### GET /pcs/{pc_id}

Get specific PC by ID.

**Authentication:** CurrentUser

**Path Parameters:**
- `pc_id` (string) - PC ID

**Response:** `200 OK` - PC object with details

**Errors:**
- `404 Not Found` - PC not found

---

### GET /pcs/{pc_id}/with-screens

Get PC with all its screens.

**Authentication:** CurrentUser

**Path Parameters:**
- `pc_id` (string) - PC ID

**Response:** `200 OK`
```json
{
  "id": "string",
  "name": "string",
  "ip_address": "string",
  "gpu_type": "string",
  "role": "string",
  "manager_id": "string",
  "screen_count": integer,
  "screens": [
    {
      "id": "string",
      "name": "string",
      "rows": integer,
      "columns": integer,
      "total_slots": integer,
      "switching_interval": integer
    }
  ],
  "last_connected": "datetime",
  "last_applied": "datetime"
}
```

---

### PUT /pcs/{pc_id}

Update PC details.

**Authentication:** AdminUser

**Path Parameters:**
- `pc_id` (string) - PC ID

**Request Body:** (all fields optional)
```json
{
  "name": "string",
  "ip_address": "string",
  "gpu_type": "string",
  "role": "string",
  "manager_id": "string"
}
```

**Response:** `200 OK` - Updated PC object

**Errors:**
- `404 Not Found` - PC or manager PC not found

---

### DELETE /pcs/{pc_id}

Delete a PC.

**Authentication:** AdminUser

**Path Parameters:**
- `pc_id` (string) - PC ID

**Response:** `204 No Content`

**Errors:**
- `404 Not Found` - PC not found

---

### POST /pcs/{pc_id}/configure-screens

Configure screens, views, and camera mappings for a PC.

**Authentication:** AdminUser

**Path Parameters:**
- `pc_id` (string) - PC ID

**Request Body:**
```json
{
  "screens": [
    {
      "layout_rows": integer,  // 1-10, view grid rows
      "layout_cols": integer,  // 1-10, view grid columns
      "num_views": integer,    // Number of views (rotation depth)
      "name": "string",
      "switch_interval": integer  // Seconds between view rotations
    }
  ],
  "camera_ids": ["string"],  // List of camera IDs to distribute
  "width": integer,   // Optional, for future use
  "height": integer   // Optional, for future use
}
```

**Response:** `200 OK`
```json
{
  "pc_id": "string",
  "screens_created": integer,
  "screens_updated": integer,
  "views_created": integer,
  "mappings_created": integer,
  "cameras_used": integer,
  "message": "string"
}
```

**Behavior:**
- Validates ALL camera IDs before any DB operations (fail-fast)
- If screen with same name exists: UPDATE/REPLACE (delete old views, create new)
- If screen name is new: CREATE new screen
- Screen rows/columns capped at 4×4 (physical display)
- View layout_rows/layout_cols can be 1-10
- Cameras distributed sequentially: Fill View 1 completely, then View 2, then Screen 2
- Empty slots are skipped (no mappings created without cameras)
- Auto-generates screen IDs: `{pc_id}_screen_{index}`
- Auto-generates view IDs: `{screen_id}_view_{view_number}`

**Errors:**
- `404 Not Found` - PC not found, or invalid camera IDs (returns `invalid_camera_ids` list)

---

## Screens, Views & Mappings

### POST /screens

Create a new screen.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "id": "string",
  "name": "string",
  "pc_id": "string",
  "rows": integer,  // 1-4, physical screen grid
  "columns": integer,  // 1-4, physical screen grid
  "switching_interval": integer  // Optional, seconds
}
```

**Response:** `201 Created` - Screen object

**Errors:**
- `404 Not Found` - PC not found
- `409 Conflict` - Screen ID already exists

---

### GET /screens

List all screens with optional filters.

**Authentication:** CurrentUser

**Query Parameters:**
- `pc_id` (string) - Filter by PC ID
- `search` (string) - Search by name

**Response:** `200 OK` - Array of screens with PC info

---

### GET /screens/count

Get screen count.

**Authentication:** CurrentUser

**Query Parameters:**
- `pc_id` (string) - Filter by PC ID

**Response:** `200 OK`
```json
{
  "total": integer
}
```

---

### GET /screens/{screen_id}

Get specific screen by ID.

**Authentication:** CurrentUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Response:** `200 OK` - Screen object

**Errors:**
- `404 Not Found` - Screen not found

---

### GET /screens/{screen_id}/with-views

Get screen with all its views.

**Authentication:** CurrentUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Response:** `200 OK` - Screen object with views array

---

### GET /screens/{screen_id}/layout

Get complete screen layout with views and camera mappings.

**Authentication:** CurrentUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Response:** `200 OK`
```json
{
  "id": "string",
  "name": "string",
  "pc_id": "string",
  "rows": integer,
  "columns": integer,
  "switching_interval": integer,
  "pc": {...},
  "view_count": integer,
  "views": [
    {
      "id": "string",
      "screen_id": "string",
      "name": "string",
      "layout_rows": integer,
      "layout_columns": integer,
      "view_number": integer,
      "mappings": [
        {
          "slot_row": integer,
          "slot_col": integer,
          "site_id": "string",
          "site_name": "string",
          "camera_id": "string",
          "camera_name": "string",
          "playing_state": boolean
        }
      ]
    }
  ]
}
```

---

### PUT /screens/{screen_id}

Update a screen.

**Authentication:** AdminUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Request Body:** (all fields optional)
```json
{
  "name": "string",
  "pc_id": "string",
  "rows": integer,
  "columns": integer,
  "switching_interval": integer
}
```

**Response:** `200 OK` - Updated screen object

---

### DELETE /screens/{screen_id}

Delete a screen (cascades to views and mappings).

**Authentication:** AdminUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Response:** `204 No Content`

---

### POST /screens/{screen_id}/views

Create a new view for a screen.

**Authentication:** AdminUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Request Body:**
```json
{
  "id": "string",
  "name": "string",
  "layout_rows": integer,  // 1-10
  "layout_columns": integer,  // 1-10
  "view_number": integer  // Unique per screen
}
```

**Response:** `201 Created` - View object

**Errors:**
- `404 Not Found` - Screen not found
- `409 Conflict` - View ID or view_number already exists

---

### GET /screens/{screen_id}/views

List all views for a screen.

**Authentication:** CurrentUser

**Path Parameters:**
- `screen_id` (string) - Screen ID

**Response:** `200 OK` - Array of view objects

---

### GET /screens/views/{view_id}

Get specific view by ID.

**Authentication:** CurrentUser

**Path Parameters:**
- `view_id` (string) - View ID

**Response:** `200 OK` - View object

**Errors:**
- `404 Not Found` - View not found

---

### GET /screens/views/{view_id}/with-mappings

Get view with all camera mappings.

**Authentication:** CurrentUser

**Path Parameters:**
- `view_id` (string) - View ID

**Response:** `200 OK` - View object with mappings array

---

### PUT /screens/views/{view_id}

Update a view.

**Authentication:** AdminUser

**Path Parameters:**
- `view_id` (string) - View ID

**Request Body:** (all fields optional)
```json
{
  "name": "string",
  "layout_rows": integer,
  "layout_columns": integer,
  "view_number": integer
}
```

**Response:** `200 OK` - Updated view object

**Errors:**
- `409 Conflict` - New view_number already exists on screen

---

### DELETE /screens/views/{view_id}

Delete a view (cascades to mappings).

**Authentication:** AdminUser

**Path Parameters:**
- `view_id` (string) - View ID

**Response:** `204 No Content`

---

### POST /screens/views/{view_id}/mappings

Create a camera mapping for a view slot.

**Authentication:** AdminUser

**Path Parameters:**
- `view_id` (string) - View ID

**Request Body:**
```json
{
  "slot_row": integer,  // 1-indexed
  "slot_col": integer,  // 1-indexed
  "site_id": "string",  // Optional
  "camera_id": "string",  // Optional
  "playing_state": boolean  // Default: false
}
```

**Response:** `201 Created` - Mapping object

**Errors:**
- `400 Bad Request` - Invalid slot position
- `404 Not Found` - View, camera, or site not found
- `409 Conflict` - Slot already has a mapping

---

### GET /screens/views/{view_id}/mappings

List all mappings for a view.

**Authentication:** CurrentUser

**Path Parameters:**
- `view_id` (string) - View ID

**Response:** `200 OK` - Array of mapping objects

---

### GET /screens/views/{view_id}/slot/{row}/{col}

Get camera assigned to a specific slot.

**Authentication:** CurrentUser

**Path Parameters:**
- `view_id` (string) - View ID
- `row` (integer) - Slot row (1-indexed)
- `col` (integer) - Slot column (1-indexed)

**Response:** `200 OK` - Camera mapping info or empty slot

**Errors:**
- `400 Bad Request` - Slot out of bounds
- `404 Not Found` - View not found

---

### PUT /screens/views/{view_id}/slot/{row}/{col}

Assign or update camera in a specific slot.

**Authentication:** AdminUser

**Path Parameters:**
- `view_id` (string) - View ID
- `row` (integer) - Slot row
- `col` (integer) - Slot column

**Query Parameters:**
- `camera_id` (string, required) - Camera ID to assign
- `site_id` (string) - Site ID (optional)
- `playing_state` (boolean, default: false) - Playing state

**Response:** `200 OK` - Mapping object

**Errors:**
- `400 Bad Request` - Invalid slot position
- `404 Not Found` - View or camera not found

---

### DELETE /screens/views/{view_id}/slot/{row}/{col}

Remove camera from a specific slot.

**Authentication:** AdminUser

**Path Parameters:**
- `view_id` (string) - View ID
- `row` (integer) - Slot row
- `col` (integer) - Slot column

**Response:** `204 No Content`

**Errors:**
- `404 Not Found` - View or mapping not found

---

### PUT /screens/mappings/{mapping_id}

Update a screen mapping.

**Authentication:** AdminUser

**Path Parameters:**
- `mapping_id` (integer) - Mapping ID

**Request Body:** (all fields optional)
```json
{
  "camera_id": "string",
  "site_id": "string",
  "playing_state": boolean
}
```

**Response:** `200 OK` - Updated mapping object

---

### DELETE /screens/mappings/{mapping_id}

Delete a screen mapping.

**Authentication:** AdminUser

**Path Parameters:**
- `mapping_id` (integer) - Mapping ID

**Response:** `204 No Content`

---

### PATCH /screens/views/{view_id}/rename

Rename a view (convenience endpoint).

**Authentication:** AdminUser

**Path Parameters:**
- `view_id` (string) - View ID

**Query Parameters:**
- `new_name` (string, required, 1-50 chars) - New view name

**Response:** `200 OK` - Updated view object

---

### GET /screens/pc/{pc_id}/all-views

Get all views with mappings for all screens of a PC.

**Authentication:** CurrentUser

**Path Parameters:**
- `pc_id` (string) - PC ID

**Response:** `200 OK` - Array of screen layouts with views and mappings

**Errors:**
- `404 Not Found` - PC not found

---

## Categories

### POST /categories

Create a new site category.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "name": "string",
  "color": "string"  // Hex color code or color name
}
```

**Response:** `201 Created`
```json
{
  "id": "uuid",
  "name": "string",
  "color": "string"
}
```

**Errors:**
- `400 Bad Request` - Category name already exists

---

### GET /categories

List all categories with site counts.

**Authentication:** CurrentUser

**Query Parameters:**
- `skip` (integer, default: 0) - Records to skip
- `limit` (integer, default: 50, max: 200) - Records to return
- `search` (string) - Search by category name

**Response:** `200 OK`
```json
[
  {
    "id": "uuid",
    "name": "string",
    "color": "string",
    "site_count": integer
  }
]
```

---

### GET /categories/count

Get total count of categories.

**Authentication:** CurrentUser

**Response:** `200 OK`
```json
{
  "total": integer
}
```

---

### GET /categories/{category_id}

Get single category by ID.

**Authentication:** CurrentUser

**Path Parameters:**
- `category_id` (UUID) - Category UUID

**Response:** `200 OK` - Category object

**Errors:**
- `404 Not Found` - Category not found

---

### PUT /categories/{category_id}

Update category details.

**Authentication:** AdminUser

**Path Parameters:**
- `category_id` (UUID) - Category UUID

**Request Body:** (all fields optional)
```json
{
  "name": "string",
  "color": "string"
}
```

**Response:** `200 OK` - Updated category object

**Errors:**
- `400 Bad Request` - New name already exists
- `404 Not Found` - Category not found

---

### DELETE /categories/{category_id}

Delete a category.

**Authentication:** AdminUser

**Path Parameters:**
- `category_id` (UUID) - Category UUID

**Query Parameters:**
- `force` (boolean, default: false) - Force delete even if sites are using this category

**Response:** `204 No Content`

**Errors:**
- `400 Bad Request` - Category has assigned sites (without force=true)
- `404 Not Found` - Category not found

---

### POST /categories/assign

Assign a category to a site.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "site_id": "string",
  "category_id": "uuid"
}
```

**Response:** `201 Created` - Mapping object

**Errors:**
- `400 Bad Request` - Mapping already exists
- `404 Not Found` - Site or category not found

---

### POST /categories/assign-bulk

Bulk assign categories to a site (replaces all existing).

**Authentication:** AdminUser

**Request Body:**
```json
{
  "site_id": "string",
  "category_ids": ["uuid"]
}
```

**Response:** `200 OK`
```json
{
  "success": true,
  "message": "Assigned N categories to site '<name>'",
  "site_id": "string",
  "category_count": integer
}
```

**Errors:**
- `404 Not Found` - Site not found or some categories not found

---

### POST /categories/unassign

Unassign a category from a site.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "site_id": "string",
  "category_id": "uuid"
}
```

**Response:** `204 No Content`

**Errors:**
- `404 Not Found` - Mapping not found

---

### GET /categories/mappings

List all category-site mappings with details.

**Authentication:** CurrentUser

**Query Parameters:**
- `skip` (integer, default: 0)
- `limit` (integer, default: 100, max: 500)

**Response:** `200 OK` - Array of mapping objects with site/category details

---

### GET /categories/site/{site_id}/categories

Get all categories assigned to a site.

**Authentication:** CurrentUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK` - Array of category objects

**Errors:**
- `404 Not Found` - Site not found

---

### GET /categories/category/{category_id}/sites

Get all sites assigned to a category.

**Authentication:** CurrentUser

**Path Parameters:**
- `category_id` (UUID) - Category UUID

**Response:** `200 OK`
```json
{
  "category_id": "uuid",
  "category_name": "string",
  "category_color": "string",
  "site_count": integer,
  "sites": [
    {
      "site_id": "string",
      "name": "string",
      "customer_id": "string"
    }
  ]
}
```

**Errors:**
- `404 Not Found` - Category not found

---

### DELETE /categories/site/{site_id}/categories

Remove all category assignments from a site.

**Authentication:** AdminUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `204 No Content`

**Errors:**
- `404 Not Found` - Site not found

---

## Configurations

### POST /configs/generate-stream-config

Generate multi-stream device configuration JSON.

**Authentication:** AdminUser

**Request Body:**
```json
{
  "screens": [
    {
      "rows": integer,     // 1-4, screen grid rows
      "columns": integer,  // 1-4, screen grid columns
      "num_views": integer  // Views per tile (rotation depth)
    }
  ],
  "camera_ids": ["string"],  // Optional, specific cameras to use
  "exclude_camera_ids": ["string"],  // Optional, cameras to exclude
  "width": integer,  // Optional, default: 640
  "height": integer,  // Optional, default: 480
  "switch_interval": integer  // Optional, default: 10 seconds
}
```

**Default Configuration (if no screens provided):**
- 4 screens
- Each screen: 3×3 grid (9 tiles)
- Each tile: 5 views
- Total: 4 × 9 × 5 = 180 camera slots

**Response:** `200 OK`
```json
{
  "config": {
    "width": integer,
    "height": integer,
    "screens": [
      {
        "id": "string",
        "display_idx": integer,
        "switchInterval": integer,
        "source_groups": [
          [
            {
              "id": "string",
              "osd_text": "string",
              "url": "string"
            }
          ]
        ]
      }
    ]
  },
  "stats": {
    "total_screens": integer,
    "total_tiles": integer,
    "total_views": integer,
    "total_camera_slots": integer,
    "cameras_used": integer,
    "empty_slots": integer,
    "unique_cameras": integer
  }
}
```

**Camera Selection Logic:**
- If `camera_ids` provided: Uses those specific cameras
- If not provided: Uses available cameras from database (ordered by site, name)
- `exclude_camera_ids`: Optionally exclude specific cameras
- If not enough cameras: Fills remaining slots with empty camera objects
- Exclusion takes priority over inclusion

**Validation:**
- All provided camera IDs must exist in database
- Returns 404 with list of invalid IDs if any don't exist

**Errors:**
- `404 Not Found` - Some camera IDs not found (returns `invalid_camera_ids` list)

---

## Snapshots

### POST /snapshots/capture/all

Trigger snapshot capture for ALL cameras (background task).

**Authentication:** AdminUser

**Response:** `200 OK`
```json
{
  "success": true,
  "message": "Snapshot capture queued for all N cameras",
  "total_cameras": integer,
  "task_id": "string",
  "status": "queued"
}
```

**Errors:**
- `404 Not Found` - No cameras found in system

---

### POST /snapshots/capture/site/{site_id}

Trigger snapshot capture for all cameras at a site (synchronous).

**Authentication:** AdminUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK`
```json
{
  "success": true,
  "message": "Snapshot capture completed for site '<name>'",
  "site_id": "string",
  "site_name": "string",
  "results": {
    "checked": integer,
    "created": integer,
    "updated": integer,
    "failed": integer,
    "skipped": integer
  }
}
```

**Errors:**
- `404 Not Found` - Site not found or no cameras

---

### POST /snapshots/capture/camera/{camera_id}

Trigger snapshot capture for a single camera.

**Authentication:** AdminUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Query Parameters:**
- `async_task` (boolean, default: false) - Queue as background task

**Response:** `200 OK`
```json
{
  "success": true,
  "message": "Snapshot captured for camera '<name>'",
  "camera_id": "string",
  "camera_name": "string",
  "result": {
    "status": "created|updated|failed",
    "reason": "string"  // If failed
  },
  "task_id": "string"  // If async_task=true
}
```

**Errors:**
- `404 Not Found` - Camera not found
- `500 Internal Server Error` - Capture failed

---

### GET /snapshots/camera/{camera_id}

Get the latest snapshot image for a camera.

**Authentication:** CurrentUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Response:** `200 OK` - PNG image binary data

**Headers:**
- `Content-Type: image/png`
- `Cache-Control: no-cache, no-store, must-revalidate`

**Errors:**
- `404 Not Found` - Camera or snapshot not found

---

### GET /snapshots/camera/{camera_id}/info

Get snapshot metadata (without image data).

**Authentication:** CurrentUser

**Path Parameters:**
- `camera_id` (string) - Camera ID

**Response:** `200 OK`
```json
{
  "camera_id": "string",
  "camera_name": "string",
  "width": integer,
  "height": integer,
  "capture_time": integer,  // Unix timestamp
  "age_seconds": integer,
  "age_hours": float,
  "image_size_bytes": integer
}
```

**Errors:**
- `404 Not Found` - Camera or snapshot not found

---

### GET /snapshots/site/{site_id}

Get snapshot metadata for all cameras at a site.

**Authentication:** CurrentUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK`
```json
{
  "site_id": "string",
  "site_name": "string",
  "total_cameras": integer,
  "cameras_with_snapshots": integer,
  "cameras_without_snapshots": integer,
  "snapshots": [
    {
      "camera_id": "string",
      "camera_name": "string",
      "has_snapshot": boolean,
      "width": integer,  // If has_snapshot
      "height": integer,
      "capture_time": integer,
      "age_seconds": integer,
      "age_hours": float,
      "image_size_bytes": integer
    }
  ]
}
```

**Errors:**
- `404 Not Found` - Site not found

---

### GET /snapshots/site/{site_id}/images

Get all snapshot images as base64-encoded JSON.

**Authentication:** CurrentUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK`
```json
{
  "site_id": "string",
  "site_name": "string",
  "total_cameras": integer,
  "cameras_with_snapshots": integer,
  "cameras_without_snapshots": integer,
  "snapshots": [
    {
      "camera_id": "string",
      "camera_name": "string",
      "has_snapshot": boolean,
      "width": integer,
      "height": integer,
      "capture_time": integer,
      "age_seconds": integer,
      "age_hours": float,
      "image_size_bytes": integer,
      "image_format": "png",
      "image_data": "string"  // Base64-encoded PNG
    }
  ]
}
```

**Errors:**
- `404 Not Found` - Site not found

---

### GET /snapshots/site/{site_id}/zip

Download all snapshots for a site as ZIP file.

**Authentication:** CurrentUser

**Path Parameters:**
- `site_id` (string) - Site ID

**Response:** `200 OK` - ZIP file binary data

**Headers:**
- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="snapshots_<site_id>_<site_name>.zip"`

**ZIP Contents:**
- Each image named: `<camera_id>_<camera_name>.png`

**Errors:**
- `404 Not Found` - Site not found, no cameras, or no snapshots available

---

### GET /snapshots/stats

Get overall snapshot statistics.

**Authentication:** CurrentUser

**Response:** `200 OK`
```json
{
  "total_cameras": integer,
  "total_snapshots": integer,
  "cameras_without_snapshots": integer,
  "outdated_snapshots": integer,  // Older than 24 hours
  "up_to_date_snapshots": integer,
  "coverage_percentage": float  // % of cameras with snapshots
}
```

---

## Error Responses

All endpoints may return the following error responses:

### 400 Bad Request
```json
{
  "detail": "Error message describing the validation issue"
}
```

### 401 Unauthorized
```json
{
  "detail": "Could not validate credentials"
}
```

### 403 Forbidden
```json
{
  "detail": "Not enough permissions"
}
```

### 404 Not Found
```json
{
  "detail": "Resource not found"
}
```

For camera ID validation errors:
```json
{
  "detail": {
    "detail": "Some camera IDs not found in database",
    "invalid_camera_ids": ["CAM001", "CAM002"]
  }
}
```

### 409 Conflict
```json
{
  "detail": "Resource already exists"
}
```

### 500 Internal Server Error
```json
{
  "detail": "Internal server error message"
}
```

---

## Authentication Headers

For all authenticated endpoints, include the JWT token:

```http
Authorization: Bearer <access_token>
```

Example with curl:
```bash
curl -X GET http://localhost:8000/api/v1/cameras \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
```

---

## Pagination

Endpoints that return lists typically support pagination:

**Query Parameters:**
- `skip` or `page` - Offset or page number
- `limit` or `per_page` - Number of items to return

**Response Format (paginated):**
```json
{
  "items": [...],
  "total": integer,
  "page": integer,
  "per_page": integer
}
```

Or simple array format with pagination parameters in query.

---

## Filtering and Search

Many list endpoints support filtering:

**Common Filters:**
- `site_id` - Filter by site
- `category_id` - Filter by category
- `role` - Filter by role
- `is_active` - Filter by active status
- `new` - Filter new items
- `search` - Full-text search (usually name, ID, or email)

---

## OpenAPI Documentation

Interactive API documentation is available at:

- **Swagger UI:** http://localhost:8000/api/v1/docs
- **ReDoc:** http://localhost:8000/api/v1/redoc

These interfaces allow you to explore all endpoints, view schemas, and test API calls directly from your browser.

---

## Health Check

### GET /health

Check API health status.

**Authentication:** None (public)

**Response:** `200 OK`
```json
{
  "status": "healthy",
  "timestamp": "datetime"
}
```
