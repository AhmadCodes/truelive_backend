# utils/config_loader.py
from database import Database, Site, Camera, PC, Screen, ScreenMapping, View
import streamlit as st
db = Database()

def load_camera_config():
    sites = db.get_sites()
    config = {"sites": {}}
    for site in sites:
        cameras = db.get_cameras_by_site(site.id)
        config["sites"][site.id] = {
            "name": site.name,
            "nvr_username": site.nvr_username,
            "nvr_password": site.nvr_password,
            "cameras": {camera.id: {"name": camera.name, "rtsp_url": camera.rtsp_url} for camera in cameras}
        }
    return config

def load_site_config():
    pcs = db.get_pcs()
    config = {"pcs": {}, "mappings": {"screen_to_cameras": {}}}
    for pc in pcs:
        screens = db.get_screens_by_pc(pc.id)
        config["pcs"][pc.id] = {
            "name": pc.name,
            "ip_address": pc.ip_address,
            "gpu_type": pc.gpu_type,
            "screens": {screen.id: {
                "name": screen.name,
                "layout": {"rows": screen.rows, "columns": screen.columns},
                "switching_interval": screen.switching_interval
            } for screen in screens}
        }
        config["mappings"]["screen_to_cameras"][pc.id] = {}
        for screen in screens:
            config["mappings"]["screen_to_cameras"][pc.id][screen.id] = {}
            views = db.get_views_by_screen(screen.id)
            for view in views:
                view_name = view.name
                mappings = db.get_screen_mappings(screen.id, view.id)
                config["mappings"]["screen_to_cameras"][pc.id][screen.id][view_name] = {
                    f"slot_{mapping.slot_row}_{mapping.slot_col}": {
                        "site_id": mapping.site_id,
                        "camera_id": mapping.camera_id
                    } for mapping in mappings
                }
    return config

def save_camera_config(config):
    for site_id, site_info in config["sites"].items():
        site = Site(site_id, site_info["name"], site_info["nvr_username"], site_info["nvr_password"])
        
        # Check if the site already exists in the database
        existing_site = db.get_site_by_id(site_id)
        if existing_site:
            db.update_site(site)  # Update the existing site
        else:
            db.add_site(site)  # Add a new site

        for camera_id, camera_info in site_info["cameras"].items():
            camera = Camera(camera_id, site_id, camera_info["name"], camera_info["rtsp_url"])
            
            # Check if the camera already exists in the database
            existing_camera = db.get_camera_by_id(camera_id)
            if existing_camera:
                db.update_camera(camera)  # Update the existing camera
            else:
                db.add_camera(camera)  # Add a new camera


def save_site_config(config):
    for pc_id, pc_info in config["pcs"].items():
        pc = PC(pc_id, pc_info["name"], pc_info["ip_address"], pc_info["gpu_type"])
        
        # Check if the PC already exists in the database
        existing_pc = db.get_pc_by_id(pc_id)
        if existing_pc:
            db.update_pc(pc)  # Update the existing PC
        else:
            db.add_pc(pc)  # Add a new PC

        for screen_id, screen_info in pc_info["screens"].items():
            screen = Screen(screen_id, pc_id, screen_info["name"], screen_info["layout"]["rows"], screen_info["layout"]["columns"], screen_info["switching_interval"])
            
            # Check if the screen already exists in the database
            existing_screen = db.get_screen_by_id(screen_id)
            if existing_screen:
                db.update_screen(screen)  # Update the existing screen
            else:
                db.add_screen(screen)  # Add a new screen

            for view_name, view_info in config["mappings"]["screen_to_cameras"][pc_id][screen_id].items():
                view_id = f"{screen_id}_{view_name}"
                view = View(
                    id=view_id,
                    screen_id=screen_id,
                    name=view_name,
                    layout_rows=screen_info["layout"]["rows"],
                    layout_columns=screen_info["layout"]["columns"]
                )
                
                # Check if the view already exists in the database
                existing_view = db.get_view_by_id(view.id)
                if existing_view:
                    db.update_view(view)  # Update the existing view
                else:
                    db.add_view(view)  # Add a new view

                # Clear existing mappings for this view
                # (This approach is safer than trying to update existing mappings)
                for row in range(screen_info["layout"]["rows"]):
                    for col in range(screen_info["layout"]["columns"]):
                        db.delete_screen_mapping(screen_id, view_id, row, col)
                
                # Add new mappings
                for slot_key, slot_info in view_info.items():
                    if slot_info and isinstance(slot_info, dict) and "site_id" in slot_info and "camera_id" in slot_info:
                        slot_row, slot_col = map(int, slot_key.split('_')[1:])
                        mapping = ScreenMapping(
                            screen_id=screen_id, 
                            view_id=view_id, 
                            slot_row=slot_row, 
                            slot_col=slot_col, 
                            site_id=slot_info["site_id"], 
                            camera_id=slot_info["camera_id"]
                        )
                        db.add_screen_mapping(mapping)
                    else:
                        st.error(f"Invalid slot_info for slot {slot_key} in view {view_name} for screen {screen_id}.")