import json
import uuid
from utils.url_processor import encode_rtsp_password
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
            n_views = len(screen_views) + 1

            for view_num in range(1, n_views):
                view_key = f"view_{view_num}"
                if view_key in screen_views and screen_views[view_key]:
                    has_data = False
                    for slot_num in range(1, screen_data["layout"]["rows"] * screen_data["layout"]["columns"] + 1):
                        slot_key = f"slot_{(slot_num - 1) // screen_data['layout']['columns'] + 1}_{(slot_num - 1) % screen_data['layout']['columns'] + 1}"
                        if screen_views[view_key].get(slot_key):
                            has_data = True
                            break
                    if has_data:
                        valid_views[view_num] = screen_views[view_key]

            if valid_views:
                for slot_num in range(1, screen_data["layout"]["rows"] * screen_data["layout"]["columns"] + 1):
                    slot_sources = []
                    row_num = (slot_num - 1) // screen_data["layout"]["columns"]
                    col_num = (slot_num - 1) % screen_data["layout"]["columns"]
                    for view_num, view_data in valid_views.items():
                        slot_key = f"slot_{row_num + 1}_{col_num + 1}"
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
                    screen_config["source_groups"].append(slot_sources)

                config["screens"].append(screen_config)

    return config

if __name__ == "__main__":
    # Load site_config.json
    with open("site_config.json", "r") as file:
        site_config = json.load(file)

    # Generate config.json
    config = generate_config(site_config)

    # Save config.json
    with open("config.json", "w") as file:
        json.dump(config, file, indent=4)