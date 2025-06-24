import json
import uuid
import logging

# Set up logger for this module
logger = logging.getLogger(__name__)

try:
    from utils.url_processor import encode_rtsp_password
    from database import Database
except ImportError:
    from url_processor import encode_rtsp_password
    from database import Database


def try_encode_rtsp_password(rtsp_url):
    """
    Safely encodes RTSP password, handling any exceptions that might occur
    
    Args:
        rtsp_url (str): The RTSP URL to encode the password for
        
    Returns:
        str: The URL with encoded password, or the original URL if an error occurred
    """
    if not rtsp_url:
        return ""
        
    try:
        return encode_rtsp_password(rtsp_url)
    except Exception as e:
        logger.error(f"Error encoding RTSP password for URL: {e}")
        # Return the original URL as a fallback
        return rtsp_url

def generate_config(site_config):
    # Initialize database connection for retrieving additional data
    try:
        db = Database()
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        # Create a basic config even if database fails
        return {
            "width": 640,
            "height": 480,
            "screens": []
        }
    
    config = {
        "width": 640,
        "height": 480,
        "screens": []
    }

    pcs = site_config.get("pcs", {})
    mappings = site_config.get("mappings", {}).get("screen_to_cameras", {})

    for pc_id, pc_data in pcs.items():
        screens = pc_data.get("screens", {})
        for screen_id, screen_data in screens.items():
            layout = screen_data.get("layout", {})
            # Get screen name/title from database
            try:
                screen = db.get_screen_by_id(screen_id)
                screen_title = screen.name if screen else f"Screen {len(config['screens']) + 1}"
            except Exception as e:
                logger.error(f"Error getting screen title for screen {screen_id}: {e}")
                screen_title = f"Screen {len(config['screens']) + 1}"
            
            screen_config = {
                "id": screen_id,
                "display_idx": len(config["screens"]),
                "switchInterval": screen_data.get("switching_interval", 10),
                "title": screen_title,
                # "layout": layout,
                "source_groups": []
            }

            screen_views = mappings.get(pc_id, {}).get(screen_id, {})
            valid_views = {}

            # Handle both numbered views and named views
            for view_key, view_data in screen_views.items():
                if view_data:  # Check if view exists
                    has_data = False
                    for slot_num in range(1, layout["rows"] * layout["columns"] + 1):
                        slot_key = f"slot_{(slot_num - 1) // layout['columns'] + 1}_{(slot_num - 1) % layout['columns'] + 1}"
                        if view_data.get(slot_key):
                            has_data = True
                            break
                    if has_data:
                        # For numbered views (view_1, view_2, etc.)
                        if view_key.startswith('view_'):
                            view_num = int(view_key.split('_')[1])
                            valid_views[view_num] = view_data
                        else:  # For named views
                            valid_views[view_key] = view_data

            if valid_views:
                for slot_num in range(1, layout["rows"] * layout["columns"] + 1):
                    slot_sources = []
                    row_num = (slot_num - 1) // layout["columns"]
                    col_num = (slot_num - 1) % layout["columns"]
                    slot_key = f"slot_{row_num + 1}_{col_num + 1}"
                    
                    for view_key, view_data in valid_views.items():
                        slot_data = view_data.get(slot_key)
                        if slot_data:
                            # Get site category and color information
                            try:
                                site_id = slot_data.get('site_id', '')
                                osd_color = "0xFFFFFFFF"  # Default white color
                                
                                if site_id:
                                    # Retrieve category color from database
                                    try:
                                        site_categories = db.get_site_categories_for_site(site_id)
                                        if site_categories and len(site_categories) > 0:
                                            # Use the first category's color (or we could prioritize them in some way)
                                            category = site_categories[0]
                                            # Format is already in 0xFFGGBBAA format
                                            osd_color = f"0x{category.color:08X}"
                                    except Exception as e:
                                        logger.error(f"Error getting site categories for site {site_id}: {e}")
                                        # Continue with default color
                            except Exception as e:
                                logger.error(f"Error processing site color information: {e}")
                                osd_color = "0xFFFFFFFF"  # Default to white on any error
                            
                            # Get LocationUris from site_cameras_layout data
                            location_uris = []
                            
                            # Get all site_cameras_layout entries for this site
                            try:
                                site_layouts = db.get_site_cameras_layout(site_id)
                            except Exception as e:
                                logger.error(f"Error getting site_cameras_layout for site {site_id}: {e}")
                                site_layouts = []
                            
                            # Extract all camera URLs for this site
                            try:
                                for site_layout in site_layouts:
                                    # Get camera info for each layout entry
                                    try:
                                        layout_camera = db.get_camera_by_id(site_layout.camera_id)
                                        if layout_camera and layout_camera.rtsp_url:
                                            # Add this camera's URL to LocationUris if not already there
                                            location_uris.append(layout_camera.rtsp_url)
                                    except Exception as e:
                                        logger.error(f"Error getting camera info for LocationUris: {e}")
                                        continue
                            except Exception as e:
                                logger.error(f"Error processing site_layouts for LocationUris: {e}")
                                # Continue with empty location_uris
                            
                            try:
                                # Create slot source entry with careful error handling for each field
                                source_entry = {
                                    "id": "",  # Default empty string
                                    "osd_text": "",  # Default empty string
                                    "url": "",  # Default empty string
                                    "osd_color": osd_color,  # Already has a default
                                    "LocationUris": location_uris,  # Already initialized
                                    "use_tcp": False  # Default false
                                }
                                
                                # Safely get ID
                                try:
                                    source_entry["id"] = f"{slot_data.get('site_id', '')}_{slot_data.get('camera_id', '')}"
                                except Exception as e:
                                    logger.error(f"Error creating ID: {e}")
                                
                                # Safely get OSD text
                                try:
                                    source_entry["osd_text"] = f"{slot_data.get('camera_name', '')} ({slot_data.get('site_name', '')})"
                                except Exception as e:
                                    logger.error(f"Error creating OSD text: {e}")
                                
                                # Safely get URL (using our safe function)
                                source_entry["url"] = try_encode_rtsp_password(slot_data.get('rtsp_url', ''))
                                
                                # Safely get use_tcp
                                try:
                                    source_entry["use_tcp"] = slot_data.get("use_tcp", False)
                                except Exception as e:
                                    logger.error(f"Error getting use_tcp: {e}")
                                
                                slot_sources.append(source_entry)
                            except Exception as e:
                                logger.error(f"Error creating slot source entry: {e}")
                                # Add a minimal working placeholder entry
                                slot_sources.append({
                                    "id": "",
                                    "osd_text": "",
                                    "url": "",
                                    "osd_color": "0xFFFFFFFF",
                                    "LocationUris": [],
                                    "use_tcp": False
                                })
                        else:
                            slot_sources.append({
                                # "location": {
                                #     "row": row_num,
                                #     "column": col_num
                                # },
                                "id": "",
                                "osd_text": "",
                                "url": "",
                                "osd_color": "0xFFFFFFFF",  
                                "LocationUris": [],
                                "use_tcp": False
                            })
                    if slot_sources:  # Only add if there are sources
                        screen_config["source_groups"].append(slot_sources)

                config["screens"].append(screen_config)

    return config


if __name__ == "__main__":
    # Load site_config.json
    with open("../site_config.json", "r") as file:
        site_config = json.load(file)

    # Generate config.json
    config = generate_config(site_config)

    # Save config.json
    with open("../modified_config.json", "w") as file:
        json.dump(config, file, indent=4)