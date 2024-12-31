#%%
import json
import uuid

def generate_config(mapping):
    config = {
        "width": 640,
        "height": 480,
        "switchInterval": 10,
        "screens": []
    }

    screen_to_cameras = mapping.get("screen_to_cameras", {})

    for screen_id, screen_data in screen_to_cameras.items():
        display_idx = 0
        screen_config = {
            "id": str(uuid.uuid4()),
            "display_idx": display_idx,
            "sources": []
        }

        for screen_key, screen_views in screen_data.items():
            for slot_num in range(1, 10):  # Slot_1_1 to Slot_3_3
                slot_sources = {}
                view_found = False

                for view_num in range(1, 3):  # view_1, view_2
                    view_key = f"view_{view_num}"
                    slot_key = f"slot_{(slot_num - 1) // 3 + 1}_{(slot_num - 1) % 3 + 1}"

                    if view_key in screen_views and screen_views[view_key] is not None:
                        slot_data = screen_views[view_key].get(slot_key, None)
                        if slot_data:
                            view_found = True
                            slot_sources[str(view_num - 1)] = {
                                "id": str(uuid.uuid4()),
                                "osd_text": f"{slot_data['camera_name']} ({slot_data['site_name']})",
                                "url": slot_data['rtsp_url']
                            }
                        else:
                            slot_sources[str(view_num - 1)] = {
                                "id": str(uuid.uuid4()),
                                "osd_text": None,
                                "url": None
                            }

                if view_found:
                    screen_config["sources"].append(slot_sources)

        if screen_config["sources"]:
            screen_config["display_idx"] = display_idx
            config["screens"].append(screen_config)
            display_idx += 1

    return config
#%%

if __name__ == "__main__":
    # Load mappings.json
    with open("mappings.json", "r") as file:
        mappings = json.load(file)

    # Generate config.json
    config = generate_config(mappings)

    # Save config.json
    with open("config.json", "w") as file:
        json.dump(config, file, indent=4)

#%%