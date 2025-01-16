# database.py
import sqlite3
from dataclasses import dataclass
from typing import List, Dict, Any

@dataclass
class Site:
    id: str
    name: str
    nvr_username: str
    nvr_password: str

@dataclass
class Camera:
    id: str
    site_id: str
    name: str
    rtsp_url: str

@dataclass
class PC:
    id: str
    name: str
    ip_address: str
    gpu_type: str

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
    view_name: str
    slot_row: int
    slot_col: int
    site_id: str
    camera_id: str
    
from dataclasses import dataclass

@dataclass
class View:
    id: str
    screen_id: str
    name: str
    layout_rows: int
    layout_columns: int



class Database:
    def __init__(self, db_path: str = 'config.db'):
        self.db_path = db_path
        self._initialize_db()

    def _initialize_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS sites (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        nvr_username TEXT NOT NULL,
                        nvr_password TEXT NOT NULL
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS cameras (
                        id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        rtsp_url TEXT NOT NULL,
                        FOREIGN KEY(site_id) REFERENCES sites(id)
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pcs (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        ip_address TEXT,
                        gpu_type TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS screens (
                        id TEXT PRIMARY KEY,
                        pc_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        rows INTEGER NOT NULL,
                        columns INTEGER NOT NULL,
                        switching_interval INTEGER NOT NULL,
                        FOREIGN KEY(pc_id) REFERENCES pcs(id)
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS screen_mappings (
                        screen_id TEXT NOT NULL,
                        view_id TEXT NOT NULL,
                        slot_row INTEGER NOT NULL,
                        slot_col INTEGER NOT NULL,
                        site_id TEXT,
                        camera_id TEXT,
                        PRIMARY KEY(screen_id, view_name, slot_row, slot_col),
                        FOREIGN KEY(view_id) REFERENCES views(id),
                        FOREIGN KEY(screen_id) REFERENCES screens(id),
                        FOREIGN KEY(site_id) REFERENCES sites(id),
                        FOREIGN KEY(camera_id) REFERENCES cameras(id)
                    )
                ''')
                
                try:
                    print("Creating views table")
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS views (
                            id TEXT PRIMARY KEY,
                            screen_id TEXT NOT NULL,
                            name TEXT NOT NULL,
                            layout_rows INTEGER NOT NULL,
                            layout_columns INTEGER NOT NULL,
                            FOREIGN KEY(screen_id) REFERENCES screens(id)
                        )
                    ''')
                    print("Views table created")
                except sqlite3.Error as e:
                    print(f"Error creating views table: {e}")
                
                conn.commit()
        except sqlite3.Error as e:
            print(f"An error occurred: {e}")
            
    def update_view_name(self, old_name: str, new_name: str,
                         screen_id: str):
        """Update the name of a view."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE views
                SET name = ?
                WHERE name = ?
                and screen_id = ?
                and id = ?
            ''', (new_name, old_name, screen_id))
            conn.commit()
            
            
    def get_view_config(self, view_id: str) -> dict:
        """Get the configuration for a specific view."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT slot_row, slot_col, site_id, camera_id
                FROM screen_mappings
                WHERE screen_id = ? AND view_name = ?
            ''', (view_id.split('_')[0], view_id.split('_')[1]))
            rows = cursor.fetchall()
            view_config = {}
            for row in rows:
                slot_row, slot_col, site_id, camera_id = row
                slot_key = f"slot_{slot_row}_{slot_col}"
                view_config[slot_key] = {
                    "site_id": site_id,
                    "camera_id": camera_id
                }
            return view_config
            
    def get_view_by_id(self, view_id: str) -> View:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, screen_id, name, layout_rows, layout_columns FROM views WHERE id = ?', (view_id,))
            row = cursor.fetchone()
            if row:
                return View(*row)
            return None
        
    def update_view(self, view: View):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE views
                SET screen_id = ?, name = ?, layout_rows = ?, layout_columns = ?
                WHERE id = ?
            ''', (view.screen_id, view.name, view.layout_rows, view.layout_columns, view.id))
            conn.commit()
            
    def delete_view(self, view_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM views WHERE id = ?', (view_id,))
            conn.commit()
            
    def add_view(self, view: View):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO views (id, screen_id, name, layout_rows, layout_columns)
                VALUES (?, ?, ?, ?, ?)
            ''', (view.id, view.screen_id, view.name, view.layout_rows, view.layout_columns))
            conn.commit()

    def get_views_by_screen(self, screen_id: str) -> List[View]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, screen_id, name, layout_rows, layout_columns FROM views WHERE screen_id = ?', (screen_id,))
            return [View(*row) for row in cursor.fetchall()]

    def add_pc(self, pc: PC):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pcs (id, name, ip_address, gpu_type)
                VALUES (?, ?, ?, ?)
            ''', (pc.id, pc.name, pc.ip_address, pc.gpu_type))
            conn.commit()
            
    def get_camera_by_id(self, camera_id: str) -> Camera:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, site_id, name, rtsp_url FROM cameras WHERE id = ?', (camera_id,))
            row = cursor.fetchone()
            if row:
                return Camera(*row)
            return None
    def get_site_by_id(self, site_id: str) -> Site:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, nvr_username, nvr_password FROM sites WHERE id = ?', (site_id,))
            row = cursor.fetchone()
            if row:
                return Site(*row)
            return None
    def update_pc(self, pc: PC):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE pcs
                SET name = ?, ip_address = ?, gpu_type = ?
                WHERE id = ?
            ''', (pc.name, pc.ip_address, pc.gpu_type, pc.id))
            conn.commit()

    def get_screen_by_id(self, screen_id: str) -> Screen:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, pc_id, name, rows, columns, switching_interval FROM screens WHERE id = ?', (screen_id,))
            row = cursor.fetchone()
            if row:
                return Screen(*row)
            return None


    def get_pc_by_id(self, pc_id: str) -> PC:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, ip_address, gpu_type FROM pcs WHERE id = ?', (pc_id,))
            row = cursor.fetchone()
            if row:
                return PC(*row)
            return None
        
        
    def get_view_config(self, view_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT slot_row, slot_col, site_id, camera_id
                FROM screen_mappings
                WHERE screen_id = ? AND view_name = ?
            ''', (view_id.split('_')[0], view_id.split('_')[1]))
            rows = cursor.fetchall()
            view_config = {}
            for row in rows:
                slot_row, slot_col, site_id, camera_id = row
                slot_key = f"slot_{slot_row}_{slot_col}"
                view_config[slot_key] = {
                    "site_id": site_id,
                    "camera_id": camera_id
                }
            return view_config

    def add_site(self, site: Site):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sites (id, name, nvr_username, nvr_password)
                VALUES (?, ?, ?, ?)
            ''', (site.id, site.name, site.nvr_username, site.nvr_password))
            conn.commit()

    def update_site(self, site: Site):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE sites
                SET name = ?, nvr_username = ?, nvr_password = ?
                WHERE id = ?
            ''', (site.name, site.nvr_username, site.nvr_password, site.id))
            conn.commit()

    def get_sites(self) -> List[Site]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, nvr_username, nvr_password FROM sites')
            return [Site(*row) for row in cursor.fetchall()]

    def add_camera(self, camera: Camera):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO cameras (id, site_id, name, rtsp_url)
                VALUES (?, ?, ?, ?)
            ''', (camera.id, camera.site_id, camera.name, camera.rtsp_url))
            conn.commit()

    def update_camera(self, camera: Camera):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE cameras
                SET site_id = ?, name = ?, rtsp_url = ?
                WHERE id = ?
            ''', (camera.site_id, camera.name, camera.rtsp_url, camera.id))
            conn.commit()

    def get_cameras_by_site(self, site_id: str) -> List[Camera]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, site_id, name, rtsp_url FROM cameras WHERE site_id = ?', (site_id,))
            return [Camera(*row) for row in cursor.fetchall()]


    def get_pcs(self) -> List[PC]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, name, ip_address, gpu_type FROM pcs')
            return [PC(*row) for row in cursor.fetchall()]

    def add_screen(self, screen: Screen):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO screens (id, pc_id, name, rows, columns, switching_interval)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (screen.id, screen.pc_id, screen.name, screen.rows, screen.columns, screen.switching_interval))
            conn.commit()

    def update_screen(self, screen: Screen):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE screens
                SET pc_id = ?, name = ?, rows = ?, columns = ?, switching_interval = ?
                WHERE id = ?
            ''', (screen.pc_id, screen.name, screen.rows, screen.columns, screen.switching_interval, screen.id))
            conn.commit()

    def get_screens_by_pc(self, pc_id: str) -> List[Screen]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, pc_id, name, rows, columns, switching_interval FROM screens WHERE pc_id = ?', (pc_id,))
            return [Screen(*row) for row in cursor.fetchall()]
    
    def add_screen_mapping(self, mapping: ScreenMapping):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Check if the mapping already exists
            cursor.execute('''
                SELECT * FROM screen_mappings
                WHERE screen_id = ? AND view_name = ? AND slot_row = ? AND slot_col = ?
            ''', (mapping.screen_id, mapping.view_name, mapping.slot_row, mapping.slot_col))
            existing_mapping = cursor.fetchone()

            if existing_mapping:
                # Update the existing mapping
                cursor.execute('''
                    UPDATE screen_mappings
                    SET site_id = ?, camera_id = ?
                    WHERE screen_id = ? AND view_name = ? AND slot_row = ? AND slot_col = ?
                ''', (mapping.site_id, mapping.camera_id, mapping.screen_id, mapping.view_name, mapping.slot_row, mapping.slot_col))
            else:
                # Insert a new mapping
                cursor.execute('''
                    INSERT INTO screen_mappings (screen_id, view_name, slot_row, slot_col, site_id, camera_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (mapping.screen_id, mapping.view_name, mapping.slot_row, mapping.slot_col, mapping.site_id, mapping.camera_id))
            conn.commit()

    def get_screen_mappings(self, screen_id: str, view_name: str) -> List[ScreenMapping]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT screen_id, view_name, slot_row, slot_col, site_id, camera_id
                FROM screen_mappings
                WHERE screen_id = ? AND view_name = ?
            ''', (screen_id, view_name))
            return [ScreenMapping(*row) for row in cursor.fetchall()]

    def update_screen_mapping(self, mapping: ScreenMapping):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE screen_mappings
                SET site_id = ?, camera_id = ?
                WHERE screen_id = ? AND view_name = ? AND slot_row = ? AND slot_col = ?
            ''', (mapping.site_id, mapping.camera_id, mapping.screen_id, mapping.view_name, mapping.slot_row, mapping.slot_col))
            conn.commit()

    def delete_screen_mapping(self, screen_id: str, view_name: str, slot_row: int, slot_col: int):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM screen_mappings
                WHERE screen_id = ? AND view_name = ? AND slot_row = ? AND slot_col = ?
            ''', (screen_id, view_name, slot_row, slot_col))
            conn.commit()
            
            
            
            
            