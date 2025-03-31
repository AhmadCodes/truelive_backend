# database.py
import sqlite3
from dataclasses import dataclass
from typing import List, Dict, Any
import uuid
import os
import logging
from typing import Optional
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class Site:
    id: str
    name: str
    nvr_username: str
    nvr_password: str
    sureview_site: bool = False
    new: bool = True


@dataclass
class Camera:
    id: str
    site_id: str
    name: str
    rtsp_url: str
    main_stream_url: str = None
    sureview_camera: bool = False
    new: bool = True


@dataclass
class PC:
    id: str
    name: str
    ip_address: str
    gpu_type: str
    role: str = "controller"  # 'manager' or 'controller'
    manager_id: str = None  # ID of the manager PC if this is a controller
    auth_token: str = None  # Authentication token
    token_expiry: int = None  # Token expiration date in ISO format
    last_connected: int = None  # Last connection timestamp in ISO format
    last_applied: int = None  # Timestamp of when configuration was last applied


@dataclass
class Screen:
    id: str
    pc_id: str
    name: str
    rows: int
    columns: int
    switching_interval: int


@dataclass
class ScreenMapping:
    screen_id: str
    view_id: str
    slot_row: int
    slot_col: int
    site_id: str
    camera_id: str
    pc_id: str = None
    playing_state: bool = False


@dataclass
class View:
    id: str
    screen_id: str
    name: str
    layout_rows: int
    layout_columns: int
    view_number: int 


class Database:
    def __init__(self, db_path: str = "config.db"):
        curr_dir = os.path.dirname(__file__)
        db_path = os.path.join(curr_dir, db_path)
        self.db_path = db_path
        logger.info(f"Database path: {db_path}")
        self._initialize_db()

    def _initialize_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Create tables in correct order to respect foreign key relationships
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sites (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        nvr_username TEXT NOT NULL,
                        nvr_password TEXT NOT NULL,
                        sureview_site boolean DEFAULT 0 NOT NULL,
                        new boolean DEFAULT 1 NOT NULL
                    )
                """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cameras (
                        id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        rtsp_url TEXT NOT NULL,
                        main_stream_url TEXT,
                        sureview_camera boolean DEFAULT 0 NOT NULL,
                        new boolean DEFAULT 1 NOT NULL,
                        FOREIGN KEY(site_id) REFERENCES sites(id)
                        
                    )
                """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pcs (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        ip_address TEXT,
                        gpu_type TEXT,
                        role TEXT DEFAULT 'controller',
                        manager_id TEXT,
                        auth_token TEXT,
                        token_expiry INTEGER,
                        last_connected INTEGER,
                        last_applied INTEGER,
                        FOREIGN KEY(manager_id) REFERENCES pcs(id)
                    )
                """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS screens (
                        id TEXT PRIMARY KEY,
                        pc_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        rows INTEGER NOT NULL,
                        columns INTEGER NOT NULL,
                        switching_interval INTEGER NOT NULL,
                        FOREIGN KEY(pc_id) REFERENCES pcs(id)
                    )
                """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS views (
                        id TEXT PRIMARY KEY,
                        screen_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        layout_rows INTEGER NOT NULL,
                        layout_columns INTEGER NOT NULL,
                        view_number INTEGER NOT NULL,
                        FOREIGN KEY(screen_id) REFERENCES screens(id)
                    )
                """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS screen_mappings (
                        pc_id TEXT NOT NULL,
                        screen_id TEXT NOT NULL,
                        view_id TEXT NOT NULL,
                        slot_row INTEGER NOT NULL,
                        slot_col INTEGER NOT NULL,
                        site_id TEXT,
                        camera_id TEXT,
                        playing_state BOOLEAN DEFAULT 0 NOT NULL,
                        PRIMARY KEY(screen_id, view_id, slot_row, slot_col),
                        FOREIGN KEY(screen_id) REFERENCES screens(id),
                        FOREIGN KEY(view_id) REFERENCES views(id),
                        FOREIGN KEY(site_id) REFERENCES sites(id),
                        FOREIGN KEY(camera_id) REFERENCES cameras(id)
                    )
                """
                )

                conn.commit()
                logger.info("Database initialized successfully.")
        except sqlite3.Error as e:
            print(f"An error occurred: {e}")
            logger.error(f"An error occurred during database initialization: {e}")

    def update_view_name(self, new_name: str,view_id: str, screen_id: str):
        """Update the name of `a view."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Update view name
            cursor.execute(
                """
                UPDATE views 
                SET name = ?
                WHERE id = ? AND screen_id = ?
            """,
                (new_name, view_id, screen_id),
            )
            conn.commit()

    def _execute_query(self, query, params=None):
        """Helper function to execute queries with error handling."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                return cursor.fetchall()  # Return results if any
        except sqlite3.Error as e:
            print(f"Database error: {e}")
            return None

    def get_view_config(
        self, pc_id: str, screen_id: str, view_id: str
    ) -> Dict[str, Any]:
        """
        Get the configuration for a specific view including all necessary fields.
        """
        if not all([pc_id, screen_id, view_id]):
            return {}

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT 
                        slot_row, 
                        slot_col, 
                        sm.site_id, 
                        sm.camera_id,
                        playing_state,
                        s.name as site_name,
                        c.name as camera_name,
                        c.rtsp_url
                    FROM screen_mappings sm
                    LEFT JOIN sites s ON sm.site_id = s.id
                    LEFT JOIN cameras c ON sm.camera_id = c.id
                    WHERE sm.pc_id = ? AND sm.screen_id = ? AND sm.view_id = ?
                """,
                    (pc_id, screen_id, view_id),
                )

                rows = cursor.fetchall()
                view_config = {}

                for row in rows:
                    (
                        slot_row,
                        slot_col,
                        site_id,
                        camera_id,
                        playing_state,
                        site_name,
                        camera_name,
                        rtsp_url,
                    ) = row
                    slot_key = f"slot_{slot_row}_{slot_col}"
                    view_config[slot_key] = {
                        "site_id": site_id,
                        "camera_id": camera_id,
                        "playing_state": bool(playing_state),
                        "site_name": site_name,
                        "camera_name": camera_name,
                        "rtsp_url": rtsp_url,
                    }

                return view_config

        except sqlite3.Error as e:
            print(f"Database error: {e}")
            return {}
        except Exception as e:
            print(f"Error getting view config: {e}")
            return {}
        
    def get_pc_config(self, pc_id: str) -> Dict[str, Any]:
        """
        Get the complete configuration for a PC including all screens and views
        in the format expected by generate_config().
        """
        if not pc_id:
            return {}

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Get PC details
                cursor.execute("SELECT id, name FROM pcs WHERE id = ?", (pc_id,))
                pc = cursor.fetchone()
                if not pc:
                    return {}
                
                # Get all screens for this PC
                cursor.execute("SELECT id, name, rows, columns, switching_interval FROM screens WHERE pc_id = ?", (pc_id,))
                screens = cursor.fetchall()
                
                # Initialize result structure
                result = {
                    "pcs": {
                        pc_id: {
                            "name": pc["name"],
                            "screens": {}
                        }
                    },
                    "mappings": {
                        "screen_to_cameras": {
                            pc_id: {}
                        }
                    }
                }
                
                # For each screen, get all views and their mappings
                for screen in screens:
                    screen_id = screen["id"]
                    
                    # Add screen info
                    result["pcs"][pc_id]["screens"][screen_id] = {
                        "name": screen["name"],
                        "layout": {
                            "rows": screen["rows"],
                            "columns": screen["columns"]
                        },
                        "switching_interval": screen["switching_interval"]  
                    }
                    
                    # Get all views for this screen
                    cursor.execute("SELECT id, name, view_number FROM views WHERE screen_id = ?", (screen_id,))
                    views = cursor.fetchall()
                    views = [dict(view) for view in views]
                    #sort views by view_number
                    views = sorted(views, key=lambda x: x["view_number"])
                    
                    # Initialize screen mappings
                    result["mappings"]["screen_to_cameras"][pc_id][screen_id] = {}
                    
                    # For each view, get the slot mappings
                    for view in views:
                        view_id = view["id"]
                        view_name = view["name"]
                        
                        # Get all slot mappings for this view
                        cursor.execute("""
                            SELECT 
                                slot_row, 
                                slot_col, 
                                sm.site_id, 
                                sm.camera_id,
                                s.name as site_name,
                                c.name as camera_name,
                                c.rtsp_url
                            FROM screen_mappings sm
                            LEFT JOIN sites s ON sm.site_id = s.id
                            LEFT JOIN cameras c ON sm.camera_id = c.id
                            WHERE sm.pc_id = ? AND sm.screen_id = ? AND sm.view_id = ?
                        """, (pc_id, screen_id, view_id))
                        
                        mappings = cursor.fetchall()
                        
                        # Store the view configuration
                        view_config = {}
                        for mapping in mappings:
                            slot_key = f"slot_{mapping['slot_row']}_{mapping['slot_col']}"
                            view_config[slot_key] = {
                                "site_id": mapping["site_id"],
                                "camera_id": mapping["camera_id"],
                                "site_name": mapping["site_name"],
                                "camera_name": mapping["camera_name"],
                                "rtsp_url": mapping["rtsp_url"]
                            }
                        
                        # Only add the view if it has mappings
                        if view_config:
                            result["mappings"]["screen_to_cameras"][pc_id][screen_id][view_name] = view_config
                
                return result
                
        except sqlite3.Error as e:
            print(f"Database error in get_pc_config: {e}")
            return {}
        except Exception as e:
            print(f"Error getting PC config: {e}")
            return {}
    
    

    def get_view_by_id(self, view_id: str) -> View:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, screen_id, name, layout_rows, layout_columns, view_number FROM views WHERE id = ?",
                (view_id,),
            )
            row = cursor.fetchone()
            if row:
                return View(*row)
            return None

    def update_view(self, view: View):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE views
                SET screen_id = ?, name = ?, layout_rows = ?, layout_columns = ?
                WHERE id = ?
            """,
                (
                    view.screen_id,
                    view.name,
                    view.layout_rows,
                    view.layout_columns,
                    view.id,
                ),
            )
            conn.commit()

    def delete_view(self, view_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM views WHERE id = ?", (view_id,))
            conn.commit()

    def add_view(self, view: View):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO views (id, screen_id, name, layout_rows, layout_columns, view_number)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    view.id,
                    view.screen_id,
                    view.name,
                    view.layout_rows,
                    view.layout_columns,
                    view.view_number,
                ),
            )
            conn.commit()

    def get_views_by_screen(self, screen_id: str) -> List[View]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, screen_id, name, layout_rows, layout_columns, view_number FROM views WHERE screen_id = ?",
                (screen_id,),
            )
            return [View(*row) for row in cursor.fetchall()]

    def add_pc(self, pc: PC):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pcs (id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    pc.id,
                    pc.name,
                    pc.ip_address,
                    pc.gpu_type,
                    pc.role,
                    pc.manager_id,
                    pc.auth_token,
                    pc.token_expiry,
                    pc.last_connected,
                    pc.last_applied,
                ),
            )
            conn.commit()

    def get_camera_by_id(self, camera_id: str) -> Camera:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, site_id, name, rtsp_url FROM cameras WHERE id = ?",
                (camera_id,),
            )
            row = cursor.fetchone()
            if row:
                return Camera(*row)
            return None

    def get_site_by_id(self, site_id: str) -> Site:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, nvr_username, nvr_password FROM sites WHERE id = ?",
                (site_id,),
            )
            row = cursor.fetchone()
            if row:
                return Site(*row)
            return None

    def update_pc(self, pc: PC):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE pcs
                SET name = ?, ip_address = ?, gpu_type = ?, role = ?, manager_id = ?, auth_token = ?, token_expiry = ?, last_connected = ?, last_applied = ?
                WHERE id = ?
            """,
                (
                    pc.name,
                    pc.ip_address,
                    pc.gpu_type,
                    pc.role,
                    pc.manager_id,
                    pc.auth_token,
                    pc.token_expiry,
                    pc.last_connected,
                    pc.last_applied,
                    pc.id,
                ),
            )
            conn.commit()

    def get_screen_by_id(self, screen_id: str) -> Screen:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, pc_id, name, rows, columns, switching_interval FROM screens WHERE id = ?",
                (screen_id,),
            )
            row = cursor.fetchone()
            if row:
                return Screen(*row)
            return None

    def get_pc_by_id(self, pc_id: str) -> Optional[PC]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied 
                FROM pcs WHERE id = ?
                """,
                (pc_id,),
            )
            row = cursor.fetchone()
            if row:
                return PC(*row)
            return None

    def add_site(self, site: Site):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sites (id, name, nvr_username, nvr_password)
                VALUES (?, ?, ?, ?)
            """,
                (site.id, site.name, site.nvr_username, site.nvr_password),
            )
            conn.commit()
        if not site.id:
            site.id = str(uuid.uuid4())  # Generate UUID if ID is missing
        return self._execute_query(
            "INSERT INTO sites (id, name, nvr_username, nvr_password) VALUES (?, ?, ?, ?)",
            (site.id, site.name, site.nvr_username, site.nvr_password),
        )

    def update_site(self, site: Site):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE sites
                SET name = ?, nvr_username = ?, nvr_password = ?
                WHERE id = ?
            """,
                (site.name, site.nvr_username, site.nvr_password, site.id),
            )
            conn.commit()

    def get_sites(self) -> List[Site]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, nvr_username, nvr_password FROM sites")
            return [Site(*row) for row in cursor.fetchall()]

    def add_camera(self, camera: Camera):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cameras (id, site_id, name, rtsp_url)
                VALUES (?, ?, ?, ?)
            """,
                (camera.id, camera.site_id, camera.name, camera.rtsp_url),
            )
            conn.commit()

    def update_camera(self, camera: Camera):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE cameras
                SET site_id = ?, name = ?, rtsp_url = ?
                WHERE id = ?
            """,
                (camera.site_id, camera.name, camera.rtsp_url, camera.id),
            )
            conn.commit()

    def get_cameras_by_site(self, site_id: str) -> List[Camera]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, site_id, name, rtsp_url FROM cameras WHERE site_id = ?",
                (site_id,),
            )
            return [Camera(*row) for row in cursor.fetchall()]

    def get_pcs(self) -> List[PC]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied 
                FROM pcs
            """
            )
            return [PC(*row) for row in cursor.fetchall()]

    def get_manager_pcs(self) -> List[PC]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied 
                FROM pcs WHERE role = 'manager'
            """
            )
            return [PC(*row) for row in cursor.fetchall()]

    # New method to get controller PCs by manager
    def get_controllers_by_manager(self, manager_id: str) -> List[PC]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied 
                FROM pcs WHERE manager_id = ?
            """,
                (manager_id,),
            )
            return [PC(*row) for row in cursor.fetchall()]

    # New method to update PC token
    def update_pc_token(self, pc_id: str, auth_token: str, token_expiry: int = int(time.time()) + 86400 ):
        """Update the auth token for a PC."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute(
                "UPDATE pcs SET auth_token = ?, token_expiry = ? WHERE id = ?",
                (auth_token, token_expiry, pc_id),
            )
            conn.commit()

    # New method to update PC connection status
    def update_pc_connection(self, pc_id: str, connected_time: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE pcs
                SET last_connected = ?
                WHERE id = ?
            """,
                (connected_time, pc_id),
            )
            conn.commit()

    # New method to verify token
    def get_pc_by_token(self, token: str) -> Optional[PC]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied 
                FROM pcs WHERE auth_token = ?
                """,
                (token,),
            )
            row = cursor.fetchone()
            if row:
                return PC(*row)
            return None

    def update_controller_manager(self, controller_id: str, manager_id: str):
        """Update the manager for a controller PC"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE pcs
                SET manager_id = ?
                WHERE id = ?
                """,
                (manager_id, controller_id),
            )
            conn.commit()

    def get_screens_by_pc(self, pc_id: str) -> List[Screen]:
        """Get all screens for a specific PC"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, pc_id, name, rows, columns, switching_interval
                FROM screens
                WHERE pc_id = ?
                """,
                (pc_id,),
            )
            return [Screen(*row) for row in cursor.fetchall()]

    def delete_screen(self, screen_id: str):
        """Delete a screen by ID"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM screens
                WHERE id = ?
                """,
                (screen_id,),
            )
            conn.commit()

    def add_screen(self, screen: Screen):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO screens (id, pc_id, name, rows, columns, switching_interval)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    screen.id,
                    screen.pc_id,
                    screen.name,
                    screen.rows,
                    screen.columns,
                    screen.switching_interval,
                ),
            )
            conn.commit()

    def update_screen(self, screen: Screen):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE screens
                SET pc_id = ?, name = ?, rows = ?, columns = ?, switching_interval = ?
                WHERE id = ?
            """,
                (
                    screen.pc_id,
                    screen.name,
                    screen.rows,
                    screen.columns,
                    screen.switching_interval,
                    screen.id,
                ),
            )
            conn.commit()

    def get_screens_by_pc(self, pc_id: str) -> List[Screen]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, pc_id, name, rows, columns, switching_interval FROM screens WHERE pc_id = ?",
                (pc_id,),
            )
            return [Screen(*row) for row in cursor.fetchall()]

    def add_screen_mapping(self, mapping: ScreenMapping):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Check if the mapping already exists
            cursor.execute(
                """
                SELECT * FROM screen_mappings
                WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
            """,
                (
                    mapping.screen_id,
                    mapping.view_id,
                    mapping.slot_row,
                    mapping.slot_col,
                ),
            )
            existing_mapping = cursor.fetchone()

            if existing_mapping:
                # Update the existing mapping
                cursor.execute(
                    """
                    UPDATE screen_mappings
                    SET site_id = ?, camera_id = ?, pc_id = ?
                    WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
                """,
                    (
                        mapping.site_id,
                        mapping.camera_id,
                        mapping.pc_id,
                        mapping.screen_id,
                        mapping.view_id,
                        mapping.slot_row,
                        mapping.slot_col,
                    ),
                )
            else:
                # Insert a new mapping
                cursor.execute(
                    """
                    INSERT INTO screen_mappings (pc_id, screen_id, view_id, slot_row, slot_col, site_id, camera_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        mapping.pc_id,
                        mapping.screen_id,
                        mapping.view_id,
                        mapping.slot_row,
                        mapping.slot_col,
                        mapping.site_id,
                        mapping.camera_id,
                    ),
                )
            conn.commit()

    def delete_screen_mapping(
        self, screen_id: str, view_id: str, slot_row: int, slot_col: int
    ):
        """Delete a screen mapping with updated view_id parameter"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM screen_mappings
                WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
            """,
                (screen_id, view_id, slot_row, slot_col),
            )
            conn.commit()

    def update_screen_mapping(self, mapping: ScreenMapping):
        """Update a screen mapping with correct view_id field"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE screen_mappings
                SET site_id = ?, camera_id = ?
                WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
            """,
                (
                    mapping.site_id,
                    mapping.camera_id,
                    mapping.screen_id,
                    mapping.view_id,
                    mapping.slot_row,
                    mapping.slot_col,
                ),
            )
            conn.commit()

    def get_screen_mappings(self, screen_id: str, view_id: str) -> List[ScreenMapping]:
        """Get screen mappings with updated view_id parameter"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT screen_id, view_id, slot_row, slot_col, site_id, camera_id, playing_state
                FROM screen_mappings
                WHERE screen_id = ? AND view_id = ?
            """,
                (screen_id, view_id),
            )
            return [ScreenMapping(*row) for row in cursor.fetchall()]

    def delete_site(self, site_id: str):
        return self._execute_query("DELETE FROM sites WHERE id = ?", (site_id,))

    def delete_camera(self, camera_id: str):
        return self._execute_query("DELETE FROM cameras WHERE id = ?", (camera_id,))

    def clear_manager_from_controllers(self, manager_id: str):
        """
        Clear the manager_id from all controller PCs that are assigned to this manager.

        Args:
            manager_id (str): The ID of the manager PC whose controllers should be unassigned
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE pcs
                SET manager_id = NULL
                WHERE manager_id = ?
                """,
                (manager_id,),
            )
            affected_rows = cursor.rowcount
            conn.commit()
            logger.info(
                f"Cleared manager {manager_id} from {affected_rows} controller PCs"
            )

    def add_screen(self, screen: Screen):
        """
        Add a new screen to the database.

        Args:
            screen (Screen): The Screen object to be added
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO screens (id, pc_id, name, rows, columns, switching_interval)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    screen.id,
                    screen.pc_id,
                    screen.name,
                    screen.rows,
                    screen.columns,
                    screen.switching_interval,
                ),
            )
            conn.commit()
            logger.info(
                f"Added screen {screen.name} (ID: {screen.id}) for PC {screen.pc_id}"
            )

    def update_screen(self, screen: Screen):
        """
        Update an existing screen in the database.

        Args:
            screen (Screen): The Screen object with updated values
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE screens
                SET pc_id = ?, name = ?, rows = ?, columns = ?, switching_interval = ?
                WHERE id = ?
                """,
                (
                    screen.pc_id,
                    screen.name,
                    screen.rows,
                    screen.columns,
                    screen.switching_interval,
                    screen.id,
                ),
            )
            if cursor.rowcount == 0:
                logger.warning(f"No screen found with ID {screen.id} to update")
            conn.commit()
            logger.info(f"Updated screen {screen.name} (ID: {screen.id})")

    def delete_pc(self, pc_id: str):
        """
        Delete a PC and all associated screens from the database.

        Args:
            pc_id (str): The ID of the PC to delete
        """
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("BEGIN TRANSACTION")
                cursor = conn.cursor()

                # First, get all screens for this PC to delete them
                cursor.execute("SELECT id FROM screens WHERE pc_id = ?", (pc_id,))
                screen_ids = [row[0] for row in cursor.fetchall()]

                # Delete screen mappings associated with these screens
                for screen_id in screen_ids:
                    cursor.execute(
                        "DELETE FROM screen_mappings WHERE screen_id = ?", (screen_id,)
                    )

                    # Delete views associated with this screen
                    cursor.execute(
                        "DELETE FROM views WHERE screen_id = ?", (screen_id,)
                    )

                # Delete all screens for this PC
                cursor.execute("DELETE FROM screens WHERE pc_id = ?", (pc_id,))

                # Finally, delete the PC itself
                cursor.execute("DELETE FROM pcs WHERE id = ?", (pc_id,))

                # Clear any controller references to this PC as manager
                cursor.execute(
                    "UPDATE pcs SET manager_id = NULL WHERE manager_id = ?", (pc_id,)
                )

                conn.commit()
                logger.info(
                    f"Deleted PC with ID {pc_id} and all its associated screens"
                )
            except sqlite3.Error as e:
                conn.rollback()
                logger.error(f"Error while deleting PC {pc_id}: {e}")
                raise

    def update_pc_connection_status(self, pc_id: str, is_connected: bool):
        """
        Update the connection status of a PC.
        If connected, updates the last_connected timestamp.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if is_connected:
                    # Use current Unix timestamp (integer)
                    current_time = int(time.time())
                    cursor.execute(
                        "UPDATE pcs SET last_connected = ? WHERE id = ?",
                        (current_time, pc_id),
                    )
                # We don't update the timestamp if not connected to preserve the last known connection time
                conn.commit()
                logger.info(f"Updated connection status for PC {pc_id}: {is_connected}")
        except Exception as e:
            logger.error(f"Failed to update PC connection status: {e}")
            return False
        return True

    def update_pc_last_applied(self, pc_id: str):
        """Update the timestamp of when configuration was last successfully applied to a PC."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Use current timestamp
            current_time = int(time.time())
            cursor.execute(
                "UPDATE pcs SET last_applied = ? WHERE id = ?",
                (current_time, pc_id),
            )
            conn.commit()
