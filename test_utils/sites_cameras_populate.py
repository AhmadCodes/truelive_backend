#!/usr/bin/env python3
# sites_cameras_populate.py

import sys
import os
import random
import logging
from typing import List, Dict, Tuple

# Add parent directory to path to import database module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database, SiteCamerasLayoutConfig, SiteCamerasLayout
from utils.logging_utils import setup_logging

# Set up logging
logger = setup_logging(logging.INFO)

def determine_grid_dimensions(camera_count: int) -> Tuple[int, int]:
    """
    Determine the grid dimensions (rows, columns) based on the number of cameras
    
    Args:
        camera_count (int): Number of cameras for the site
        
    Returns:
        Tuple[int, int]: (rows, columns)
    """
    if camera_count <= 0:
        return (0, 0)
    elif camera_count <= 2:
        return (1, 2)  # 1 row x 2 columns
    elif camera_count <= 4:
        return (2, 2)  # 2x2 grid
    elif camera_count <= 6:
        return (2, 3)  # 2x3 grid
    elif camera_count <= 9:
        return (3, 3)  # 3x3 grid
    elif camera_count <= 12:
        return (3, 4)  # 3x4 grid
    else:
        return (4, 4)  # 4x4 grid (maximum)

def populate_site_cameras(db: Database) -> None:
    """
    Populate camera layouts for all sites in the database
    
    Args:
        db (Database): Database instance
    """
    # Get all sites
    sites = db.get_sites()
    logger.info(f"Found {len(sites)} sites in the database")
    
    for site in sites:
        # Get all cameras for this site
        cameras = db.get_cameras_by_site(site.id)
        logger.info(f"Site {site.name} has {len(cameras)} cameras")
        
        if not cameras:
            logger.info(f"Skipping site {site.name} as it has no cameras")
            continue
        
        # Determine grid dimensions based on camera count
        num_cameras = len(cameras)
        rows, cols = determine_grid_dimensions(num_cameras)
        
        # For sites with more than 16 cameras, randomly select 16 cameras
        if num_cameras > 16:
            logger.info(f"Site {site.name} has {num_cameras} cameras, selecting 16 randomly")
            cameras = random.sample(cameras, 16)
            num_cameras = 16
            rows = 4
            cols = 4
        
        # Create or update layout configuration
        config = SiteCamerasLayoutConfig(
            site_id=site.id,
            site_name=site.name,
            n_rows=rows,
            n_cols=cols
        )
        
        # Add layout configuration to database
        if db.add_site_cameras_layout_config(config):
            logger.info(f"Added layout configuration for site {site.name}: {rows}x{cols}")
        else:
            logger.error(f"Failed to add layout configuration for site {site.name}")
            continue
        
        # First delete any existing camera layout for this site to avoid conflicts
        db.delete_site_cameras_layout(site.id)
        
        # Add cameras to layout
        camera_index = 0
        for row in range(1, rows + 1):
            for col in range(1, cols + 1):
                # Break if we've assigned all cameras
                if camera_index >= num_cameras:
                    break
                
                camera = cameras[camera_index]
                
                # Create layout item
                layout_item = SiteCamerasLayout(
                    site_id=site.id,
                    site_name=site.name,
                    slot_row=row,
                    slot_col=col,
                    camera_id=camera.id
                )
                
                # Add layout item to database
                if db.add_site_cameras_layout(layout_item):
                    logger.info(f"Added camera {camera.name} to site {site.name} at position ({row}, {col})")
                else:
                    logger.error(f"Failed to add camera {camera.name} to site {site.name} at position ({row}, {col})")
                
                camera_index += 1
        
        logger.info(f"Completed layout setup for site {site.name}")

def main():
    # Initialize database
    try:
        db = Database()
        logger.info("Connected to database")
        
        # Populate site cameras
        populate_site_cameras(db)
        
        logger.info("Camera layout population completed successfully!")
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == "__main__":
    main()
