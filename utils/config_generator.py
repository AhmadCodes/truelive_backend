import json
import uuid
try:
    from utils.url_processor import encode_rtsp_password
except ImportError:
    from url_processor import encode_rtsp_password

def generate_config(site_config):
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
            screen_config = {
                "id": screen_id,
                "display_idx": len(config["screens"]),
                "switchInterval": screen_data.get("switching_interval", 10),
                "layout": layout,
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
                            slot_sources.append({
                                "location": {
                                    "row": row_num,
                                    "column": col_num
                                },
                                "id": f"{slot_data['site_id']}_{slot_data['camera_id']}",
                                "osd_text": f"{slot_data['camera_name']} ({slot_data['site_name']})",
                                "url": encode_rtsp_password(slot_data['rtsp_url']),
                                "mainstream_url": encode_rtsp_password(slot_data['rtsp_url'])
                            })
                        else:
                            slot_sources.append({
                                "location": {
                                    "row": row_num,
                                    "column": col_num
                                },
                                "id": None,
                                "osd_text": None,
                                "url": None
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