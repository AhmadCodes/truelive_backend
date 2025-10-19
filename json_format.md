# WebSocket Configuration JSON Format

This document describes the detailed structure of the JSON configuration sent from the Shomer Portal backend to PC clients via WebSocket for deploying camera display layouts.

## Table of Contents

1. [Overview](#overview)
2. [Root Structure](#root-structure)
3. [Screen Object](#screen-object)
4. [Source Groups Structure](#source-groups-structure)
5. [Camera Object](#camera-object)
6. [Empty Tiles](#empty-tiles)
7. [Complete Examples](#complete-examples)
8. [Data Flow](#data-flow)

---

## Overview

The configuration JSON defines:
- **Display resolution** (width/height)
- **Screen layouts** (one or more monitors)
- **Camera grid configurations** (e.g., 4x4, 3x4, 2x2)
- **View rotation** (multiple cameras per tile that switch at intervals)
- **Multi-site support** (cameras from different sites with color-coded OSD)

**Key terminology:**
- **Tile**: A single cell in the camera grid (e.g., one cell in a 4x4 grid)
- **Tile Array**: Array of camera objects for a single tile
- **Camera Object**: Configuration for one camera stream in a view
- **View**: A specific camera configuration that appears during rotation
- **View Rotation**: Sequential display of cameras within a tile based on `switchInterval`

---

## Root Structure

```json
{
  "width": 640,
  "height": 480,
  "screens": [...]
}
```

### Fields

| Field | Type | Description | Required |
|-------|------|-------------|----------|
| `width` | integer | Display width in pixels | Yes |
| `height` | integer | Display height in pixels | Yes |
| `screens` | array | Array of screen objects (one per monitor) | Yes |

**Example:**
```json
{
  "width": 640,
  "height": 480,
  "screens": [
    { /* screen 1 config */ },
    { /* screen 2 config */ }
  ]
}
```

---

## Screen Object

Each screen represents a physical monitor or display output.

```json
{
  "id": "pcc6bc_screen_17b63139",
  "display_idx": 0,
  "switchInterval": 10,
  "title": "Monitor 1",
  "source_groups": [...]
}
```

### Fields

| Field | Type | Description | Source | Example |
|-------|------|-------------|--------|---------|
| `id` | string | Unique screen identifier | Database: `pc{id}_screen_{screen_id}` | `"pcc6bc_screen_17b63139"` |
| `display_idx` | integer | Display index (0-based) | Database: Screen.display_idx | `0` |
| `switchInterval` | integer | Seconds between view rotations | Database: Screen.switch_interval | `10` |
| `title` | string | Human-readable screen name | Database: Screen.name | `"Monitor 1"` |
| `source_groups` | array | Array of tile arrays | Generated from ScreenMapping | See below |

### Display Index

- `0` = Primary monitor
- `1` = Secondary monitor
- `2` = Third monitor, etc.

Used for multi-monitor setups where a single PC drives multiple displays.

---

## Source Groups Structure

`source_groups` is the most complex and critical part of the configuration. It defines the camera grid layout.

### Structure

```
source_groups = Array of Tile Arrays
│
├─ Tile Array [0]     ← First tile in grid (top-left)
│  ├─ Camera Object [0]   ← View 1
│  ├─ Camera Object [1]   ← View 2
│  └─ Camera Object [2]   ← View 3
│
├─ Tile Array [1]     ← Second tile
│  └─ Camera Object [0]   ← Single view (no rotation)
│
├─ Tile Array [2]     ← Third tile
│  ├─ Camera Object [0]   ← View 1
│  └─ Camera Object [1]   ← View 2
│
└─ ... (continues for all tiles in grid)
```

### Grid Layout Mapping

The index in `source_groups` corresponds to grid position in **row-major order**:

**4x4 Grid (16 tiles):**
```
[0]  [1]  [2]  [3]
[4]  [5]  [6]  [7]
[8]  [9]  [10] [11]
[12] [13] [14] [15]
```

**3x4 Grid (12 tiles):**
```
[0]  [1]  [2]  [3]
[4]  [5]  [6]  [7]
[8]  [9]  [10] [11]
```

**2x2 Grid (4 tiles):**
```
[0] [1]
[2] [3]
```

### View Rotation Logic

Cameras within a tile array rotate sequentially:

1. At time T=0: Display camera object [0]
2. At time T=switchInterval: Display camera object [1]
3. At time T=2×switchInterval: Display camera object [2]
4. At time T=3×switchInterval: Wrap back to camera object [0]

**Example:** With `switchInterval: 10` and 3 camera objects:
- 0-10s: Show camera [0]
- 10-20s: Show camera [1]
- 20-30s: Show camera [2]
- 30-40s: Show camera [0] (repeat)

### Number of Tile Arrays

- **Must match grid size**: For a 4x4 grid, provide 16 tile arrays
- **Partial grids allowed**: Unused tiles should contain empty camera objects
- **Layout defined in database**: Grid size comes from View.rows × View.columns

---

## Camera Object

Each camera object represents one camera stream configuration.

```json
{
  "id": "10538_193628",
  "osd_text": " 13-21 Lexington 132 ( 13-21 Lexington)",
  "url": "rtsp://admin:Usvirtualguard1@66.108.96.217:8554/Streaming/channels/202",
  "osd_color": "0xFFDC42FF",
  "LocationUris": [
    "rtsp://admin:Usvirtualguard1@66.108.96.217:8554/Streaming/channels/202",
    "rtsp://admin:Usvirtualguard1@66.108.96.217:8554/Streaming/channels/402"
  ],
  "use_tcp": false
}
```

### Fields

| Field | Type | Description | Source | Example |
|-------|------|-------------|--------|---------|
| `id` | string | Camera identifier | Database: `{site_id}_{camera_id}` | `"10538_193628"` |
| `osd_text` | string | On-screen display text | Format: `"{camera_name} ({site_name})"` | `"Camera 1 (Site A)"` |
| `url` | string | RTSP stream URL (URL-encoded) | Database: Camera.rtsp_url (processed via `url_processor.py`) | `"rtsp://..."` |
| `osd_color` | string | Hex color for OSD text | Database: SiteCategory.color (via SiteCategoryMapping) | `"0xFFDC42FF"` |
| `LocationUris` | array | Array of RTSP URLs for site | Database: SiteCamerasLayout for this site | `["rtsp://...", ...]` |
| `use_tcp` | boolean | Use TCP transport for RTSP | Database: Camera.use_tcp or default false | `false` |

### Field Details

#### `id` - Camera Identifier

- **Format**: `"{site_id}_{camera_id}"`
- **Purpose**: Unique identifier for logging, debugging, and client-side tracking
- **Example**: `"10538_193628"` means site ID 10538, camera ID 193628

#### `osd_text` - On-Screen Display Text

- **Format**: `"{camera_name} ({site_name})"`
- **Purpose**: Text overlay shown on video stream
- **Data source**:
  - Camera name from `Camera.name`
  - Site name from `Site.name`
- **Example**: `"Front Door (123 Main Street)"`
- **Note**: May have leading/trailing spaces in actual data

#### `url` - RTSP Stream URL

- **Format**: `rtsp://username:password@host:port/path`
- **Processing**: Passwords are URL-encoded via `app/utils/url_processor.py`
- **Special characters**: Characters like `@`, `:`, `/` in passwords are encoded
  - Example: Password `12345@ny` becomes `12345%40ny`
- **Data source**: `Camera.rtsp_url` from database
- **Example**: `"rtsp://admin:12345%40ny@110emerson.ddns.net:8554/Streaming/Channels/1302"`

#### `osd_color` - OSD Text Color

- **Format**: Hex color string `"0xFFRRGGBB"` (ARGB format)
  - `FF` = Alpha (opacity, always 255)
  - `RR` = Red component
  - `GG` = Green component
  - `BB` = Blue component
- **Purpose**: Color-code cameras by site category for easy visual identification
- **Data source**:
  1. Site → SiteCategoryMapping → SiteCategory
  2. `SiteCategory.color` field
- **Examples**:
  - `"0xFFDC42FF"` = Bright purple
  - `"0xFFFF0006"` = Bright red
  - `"0xFFFCFF00"` = Bright yellow
  - `"0xFFFFFFFF"` = White (default/empty)

#### `LocationUris` - Site Camera Array

- **Format**: Array of RTSP URL strings
- **Purpose**: Provides all cameras for a site to enable site-wide camera cycling
- **Data source**: `SiteCamerasLayout` table for this camera's site
- **Use case**: Allows client to quickly switch between all cameras at a site
- **Example**:
  ```json
  [
    "rtsp://admin:pass@ip:554/Streaming/channels/102",
    "rtsp://admin:pass@ip:554/Streaming/channels/202",
    "rtsp://admin:pass@ip:554/Streaming/channels/302"
  ]
  ```

#### `use_tcp` - TCP Transport Flag

- **Format**: Boolean
- **Purpose**: Force RTSP over TCP instead of UDP
- **Use case**: Helps with firewall/NAT traversal or when UDP packets are dropped
- **Default**: `false` (use UDP)
- **Data source**: `Camera.use_tcp` field or default

---

## Empty Tiles

To leave a tile blank (no camera displayed), use an empty camera object:

```json
{
  "id": "",
  "osd_text": "",
  "url": "",
  "osd_color": "0xFFFFFFFF",
  "LocationUris": [],
  "use_tcp": false
}
```

### When to Use Empty Tiles

- **Partial grid layouts**: When not all tiles in grid are used (e.g., 10 cameras in 4x4 grid)
- **Asymmetric layouts**: Creating custom layouts with blank spaces
- **View rotation gaps**: Intentionally leaving a tile blank during certain rotation phases

### Important Notes

- Empty tiles still occupy a position in `source_groups` array
- Grid layout must still be complete (e.g., 16 tiles for 4x4)
- Color is white (`0xFFFFFFFF`) for empty tiles
- All string fields are empty strings, not null

---

## Complete Examples

### Example 1: Simple 2x2 Grid with No Rotation

```json
{
  "width": 1920,
  "height": 1080,
  "screens": [
    {
      "id": "pc123_screen_abc",
      "display_idx": 0,
      "switchInterval": 0,
      "title": "Main Monitor",
      "source_groups": [
        [
          {
            "id": "101_201",
            "osd_text": "Front Entrance (Building A)",
            "url": "rtsp://admin:pass@192.168.1.100:554/stream1",
            "osd_color": "0xFFFF0000",
            "LocationUris": ["rtsp://admin:pass@192.168.1.100:554/stream1"],
            "use_tcp": false
          }
        ],
        [
          {
            "id": "101_202",
            "osd_text": "Back Door (Building A)",
            "url": "rtsp://admin:pass@192.168.1.100:554/stream2",
            "osd_color": "0xFFFF0000",
            "LocationUris": ["rtsp://admin:pass@192.168.1.100:554/stream2"],
            "use_tcp": false
          }
        ],
        [
          {
            "id": "102_301",
            "osd_text": "Parking Lot (Building B)",
            "url": "rtsp://admin:pass@192.168.1.101:554/stream1",
            "osd_color": "0xFF00FF00",
            "LocationUris": ["rtsp://admin:pass@192.168.1.101:554/stream1"],
            "use_tcp": false
          }
        ],
        [
          {
            "id": "",
            "osd_text": "",
            "url": "",
            "osd_color": "0xFFFFFFFF",
            "LocationUris": [],
            "use_tcp": false
          }
        ]
      ]
    }
  ]
}
```

### Example 2: Single Tile with 3-Camera Rotation

```json
{
  "width": 1920,
  "height": 1080,
  "screens": [
    {
      "id": "pc456_screen_def",
      "display_idx": 0,
      "switchInterval": 15,
      "title": "Rotating Cameras",
      "source_groups": [
        [
          {
            "id": "103_401",
            "osd_text": "Camera 1 (Site C)",
            "url": "rtsp://admin:pass@192.168.1.102:554/stream1",
            "osd_color": "0xFFFF00FF",
            "LocationUris": [
              "rtsp://admin:pass@192.168.1.102:554/stream1",
              "rtsp://admin:pass@192.168.1.102:554/stream2",
              "rtsp://admin:pass@192.168.1.102:554/stream3"
            ],
            "use_tcp": false
          },
          {
            "id": "103_402",
            "osd_text": "Camera 2 (Site C)",
            "url": "rtsp://admin:pass@192.168.1.102:554/stream2",
            "osd_color": "0xFFFF00FF",
            "LocationUris": [
              "rtsp://admin:pass@192.168.1.102:554/stream1",
              "rtsp://admin:pass@192.168.1.102:554/stream2",
              "rtsp://admin:pass@192.168.1.102:554/stream3"
            ],
            "use_tcp": false
          },
          {
            "id": "103_403",
            "osd_text": "Camera 3 (Site C)",
            "url": "rtsp://admin:pass@192.168.1.102:554/stream3",
            "osd_color": "0xFFFF00FF",
            "LocationUris": [
              "rtsp://admin:pass@192.168.1.102:554/stream1",
              "rtsp://admin:pass@192.168.1.102:554/stream2",
              "rtsp://admin:pass@192.168.1.102:554/stream3"
            ],
            "use_tcp": false
          }
        ]
      ]
    }
  ]
}
```

**Behavior**: Every 15 seconds, the display switches to the next camera in the tile.

### Example 3: 4x4 Grid with Mixed Rotation

See `json_format.json` for a real-world example with:
- 12 tiles in a 4x4 grid (some empty)
- Multiple cameras per tile (2 cameras rotating)
- Different sites with color-coded OSD
- URL-encoded passwords
- Site camera arrays in LocationUris

---

## Data Flow

### From Database to JSON

1. **Query Screen Configuration**
   - Get Screen record (id, name, display_idx, switch_interval)
   - Get associated View record (rows, columns)
   - Get ScreenMapping records (camera assignments to grid positions)

2. **Query Camera Data**
   - For each ScreenMapping:
     - Get Camera record (id, name, rtsp_url, use_tcp)
     - Get Site record (id, name)
     - Get SiteCategory color via SiteCategoryMapping
     - Get SiteCamerasLayout records for LocationUris

3. **Transform to JSON** (via `app/services/config_generator.py`)
   - Create screen object with metadata
   - Build source_groups array:
     - For each grid position (row × column):
       - Get cameras mapped to this position
       - Create camera objects with:
         - id: `f"{site_id}_{camera_id}"`
         - osd_text: `f"{camera_name} ({site_name})"`
         - url: URL-encode camera RTSP URL
         - osd_color: Get from site category
         - LocationUris: Get all cameras for this site
   - Sort and organize into proper grid layout

4. **Send via WebSocket**
   - Serialize JSON
   - Send to target PC client via Socket.IO `message` event
   - PC client applies configuration and updates display

### Configuration Update Flow

```
Admin UI → FastAPI Endpoint → Config Generator → WebSocket Server → PC Client
   ↓             ↓                    ↓                 ↓              ↓
Update DB → Trigger Deploy → Build JSON → Send Message → Apply Layout
```

### URL Processing

Passwords with special characters are URL-encoded:

| Original | Encoded | Character |
|----------|---------|-----------|
| `@` | `%40` | At sign |
| `:` | `%3A` | Colon |
| `/` | `%2F` | Slash |
| `#` | `%23` | Hash |
| `?` | `%3F` | Question mark |

**Example:**
- Original: `rtsp://admin:12345@ny@host:554/stream`
- Encoded: `rtsp://admin:12345%40ny@host:554/stream`

This encoding is handled by `app/utils/url_processor.py` before generating the JSON.

---

## Notes and Best Practices

### Grid Layout Considerations

- **Always provide complete grids**: Even if some tiles are empty, include them in `source_groups`
- **Maintain row-major order**: Tiles must be in order [0], [1], [2]... corresponding to left-to-right, top-to-bottom
- **Consistent tile counts**: 4x4 = 16, 3x4 = 12, 2x2 = 4, etc.

### View Rotation

- **switchInterval = 0**: No rotation, show only first camera in each tile
- **switchInterval > 0**: Rotate through cameras at specified interval
- **Smooth transitions**: PC client handles fade/transition effects

### Color Coding

- Use distinct colors for different site categories
- Helps operators quickly identify which site a camera belongs to
- Standard format: `"0xFFRRGGBB"` (ARGB)

### LocationUris Usage

- Provides quick access to all site cameras
- Useful for client-side site camera cycling
- All URLs should be from same site as the primary camera
- Generated from `SiteCamerasLayout` table

### Performance Considerations

- **Large grids**: 4x4 with rotation = up to 16 concurrent streams
- **Switch intervals**: Balance between update frequency and visual stability
- **Network bandwidth**: Multiple RTSP streams require significant bandwidth
- **URL encoding**: Always encode passwords before transmission

---

## Validation Checklist

When generating or validating configuration JSON:

- [ ] Root object has `width`, `height`, and `screens` array
- [ ] Each screen has `id`, `display_idx`, `switchInterval`, `title`, and `source_groups`
- [ ] `source_groups` length matches grid size (rows × columns)
- [ ] Each tile array contains at least 1 camera object
- [ ] Empty tiles use proper empty object structure
- [ ] Camera IDs follow format `{site_id}_{camera_id}`
- [ ] OSD text follows format `{camera_name} ({site_name})`
- [ ] RTSP URLs are properly URL-encoded
- [ ] OSD colors are valid hex format `0xFFRRGGBB`
- [ ] LocationUris arrays contain valid RTSP URLs
- [ ] `use_tcp` is boolean (not string)

---

## Related Files

- **Generator**: `app/services/config_generator.py` - Builds JSON from database
- **URL Processor**: `app/utils/url_processor.py` - Handles RTSP URL encoding
- **WebSocket Server**: `app/services/websocket_server.py` - Sends config to clients
- **Example**: `json_format.json` - Real-world configuration example
- **Documentation**: `CLAUDE.md` - Project overview and architecture

---

*Last updated: 2025-10-19*
