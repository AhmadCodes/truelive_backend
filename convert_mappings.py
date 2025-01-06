import json
import uuid

def generate_config(mappings):
    config = {
        "width": 640,
        "height": 480,
        "switchInterval": 10,
        "screens": []
    }

    display_idx = 0

    for screen_id, screen_data in mappings.items():
        for screen_key, screen_views in screen_data.items():
            screen_config = {
                "id": str(uuid.uuid4()),
                "display_idx": display_idx,
                "sources": []
            }
            
            # First, identify which views have any data
            valid_views = {}
            n_views = len(screen_views) + 1
            
            for view_num in range(1, n_views):
                view_key = f"view_{view_num}"
                if view_key in screen_views and screen_views[view_key]:
                    has_data = False
                    for slot_num in range(1, 10):
                        slot_key = f"slot_{(slot_num - 1) // 3 + 1}_{(slot_num - 1) % 3 + 1}"
                        if screen_views[view_key].get(slot_key):
                            has_data = True
                            break
                    if has_data:
                        valid_views[view_num] = screen_views[view_key]
            
            # Only proceed if we found any valid views
            if valid_views:
                # Process all 9 slots
                for slot_num in range(1, 10):
                    slot_sources = {}
                    
                    # For each valid view
                    for view_num, view_data in valid_views.items():
                        slot_key = f"slot_{(slot_num - 1) // 3 + 1}_{(slot_num - 1) % 3 + 1}"
                        slot_data = view_data.get(slot_key)
                        
                        if slot_data:
                            slot_sources[str(view_num - 1)] = {
                                "id": f"{slot_data['site_id']}_{slot_data['camera_id']}",
                                "osd_text": f"{slot_data['camera_name']} ({slot_data['site_name']})",
                                "url": slot_data['rtsp_url']
                            }
                        else:
                            slot_sources[str(view_num - 1)] = {
                                "id": None,
                                "osd_text": None,
                                "url": None
                            }
                    
                    if slot_sources:  # Only add if we have sources (even if they're all null)
                        screen_config["sources"].append(slot_sources)
                
                # Add screen only if it has sources
                if screen_config["sources"]:
                    config["screens"].append(screen_config)
                    display_idx += 1

    return config

if __name__ == "__main__":
    # Load mappings.json
    with open("mappings.json", "r") as file:
        mappings = json.load(file)

    # Generate config.json
    config = generate_config(mappings)

    # Save config.json
    with open("config.json", "w") as file:
        json.dump(config, file, indent=4)