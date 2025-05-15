# database.py
import sqlite3
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple, Union
import uuid
import os
import logging
from typing import Optional
import time
import json
import hashlib
import secrets
import string
import pandas as pd
from datetime import datetime, timedelta

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


@dataclass
class User:
    id: str
    username: str
    email: str
    password_hash: str
    role: str  # 'super_admin', 'admin', or 'user'
    created_at: int
    last_login: int = None
    is_active: bool = True
    invite_token: str = None
    token_expiry: int = None


@dataclass
class Screenshot:
    camera_id: str
    image: bytes
    height: int
    width: int
    capture_time: int  # Unix timestamp


class NullCursor:
    """A null object for cursor to safely handle failures"""
    def fetchone(self):
        return None
    
    def fetchall(self):
        return []
        
    def __iter__(self):
        return iter([])


class Database:
    def __init__(self, db_path: str = "config.db"):
        curr_dir = os.path.dirname(__file__)
        db_path = os.path.join(curr_dir, db_path)
        self.db_path = db_path
        logger.info(f"Database path: {db_path}")
        # Don't keep a persistent connection - create new ones as needed
        self._initialize_db()

    def _initialize_db(self):
        """Initialize the database with required tables"""
        try:
            # Create tables in correct order to respect foreign key relationships
            # Sites table
            self._execute_query(
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

            # Cameras table
            self._execute_query(
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

            # Screenshots table
            self._execute_query(
                    """
                    CREATE TABLE IF NOT EXISTS screenshots (
                        camera_id TEXT PRIMARY KEY,
                        image BLOB NOT NULL,
                        height INTEGER NOT NULL,
                        width INTEGER NOT NULL,
                        capture_time INTEGER NOT NULL,
                        FOREIGN KEY(camera_id) REFERENCES cameras(id)
                    )
                """
                )

            # PCs table
            self._execute_query(
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

            # Screens table
            self._execute_query(
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

            # Views table
            self._execute_query(
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

            # Screen mappings table
            self._execute_query(
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

            # Create users table if it doesn't exist
            users_table = """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT,
                last_login TEXT
            )
            """
            self._execute_query(users_table)
            
            # Create invitation tokens table
            invitation_tokens_table = """
            CREATE TABLE IF NOT EXISTS invitation_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_used INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """
            self._execute_query(invitation_tokens_table)
            
            # Create default super admin user if no users exist
            self.create_default_user()
            
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise

    def _hash_password(self, password, salt=None):
        """Hash a password with SHA-256 and return salt$hash
        
        Args:
            password (str): The password to hash
            salt (str, optional): The salt to use. If None, a random salt is generated.
            
        Returns:
            str: The hashed password with salt in format salt$hash
        """
        if not salt:
            # Generate a random salt if not provided
            salt = secrets.token_hex(16)
            
        # Create a SHA-256 hash of the password and salt
        hash_obj = hashlib.sha256((password + salt).encode('utf-8')).hexdigest()
        
        # Return the salt and hash in format salt$hash
        return f"{salt}${hash_obj}"

    def verify_password(self, password, stored_password_hash):
        """Verify a password against a stored hash
        
        Args:
            password (str): The password to verify
            stored_password_hash (str): The stored password hash (in format salt$hash)
            
        Returns:
            bool: Whether the password matches the hash
        """
        try:
            if not password or not stored_password_hash:
                logger.warning("Empty password or hash provided to verify_password")
                return False
            
            # Split the stored hash into salt and hash components
            if '$' not in stored_password_hash:
                logger.warning("Invalid password hash format (missing $ separator)")
                return False
            
            salt, stored_hash = stored_password_hash.split('$', 1)
            
            # Hash the provided password with the same salt
            password_hash = self._hash_password(password, salt)
            
            # Extract just the hash part from the salt$hash format
            computed_hash = password_hash.split('$', 1)[1] if '$' in password_hash else password_hash
            
            # Compare the hashes
            return secrets.compare_digest(stored_hash, computed_hash)
        except Exception as e:
            logger.error(f"Error verifying password: {e}")
            return False
    
    def add_user(self, user: User):
        """Add a new user to the database"""
        query = '''
            INSERT INTO users 
            (user_id, username, email, password, role, created_at, last_login, is_active, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        self._execute_query(query, (
            user.id, 
            user.username, 
            user.email, 
            user.password_hash, 
            user.role, 
            user.created_at,
            user.last_login,
            1 if user.is_active else 0,
            None
        ))
    
    def update_user(self, user_id: str, username: str = None, email: str = None, password: str = None, role: str = None, is_active: bool = None, last_login: str = None):
        """
        Update an existing user with flexible parameter updates
        
        Args:
            user_id (str): The ID of the user to update (required)
            username (str, optional): New username
            email (str, optional): New email
            password (str, optional): New password (will be hashed)
            role (str, optional): New role
            is_active (bool, optional): New active status
            last_login (str, optional): New last login time
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not user_id:
            logger.warning("Attempted to update user with empty user_id")
            return False
            
        try:
            # Get current user data to only update provided fields
            current_user = self.get_user_by_id(user_id)
            if not current_user:
                logger.warning(f"Attempted to update non-existent user: {user_id}")
                return False
                
            # Prepare update fields and values
            update_fields = []
            params = []
            
            # Only update fields that were provided
            if username is not None:
                update_fields.append("username = ?")
                params.append(username)
                
            if email is not None:
                update_fields.append("email = ?")
                params.append(email)
                
            if password is not None:
                # Hash the new password if provided
                password_hash = self._hash_password(password)
                update_fields.append("password = ?")
                params.append(password_hash)
                
            if role is not None:
                update_fields.append("role = ?")
                params.append(role)
                
            if last_login is not None:
                update_fields.append("last_login = ?")
                params.append(last_login)
                
            if is_active is not None:
                update_fields.append("is_active = ?")
                params.append(1 if is_active else 0)
                
            # Return early if no fields to update
            if not update_fields:
                logger.warning(f"No fields provided to update for user: {user_id}")
                return True  # Not an error, just nothing to do
                
            # Build and execute update query
            query = f"UPDATE users SET {', '.join(update_fields)} WHERE user_id = ?"
            params.append(user_id)
            
            self._execute_query(query, tuple(params))
            logger.info(f"Updated user: {user_id}")
            
            return True
        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}")
            return False
    
    def delete_user(self, user_id: str) -> bool:
        """
        Delete a user from the database
        
        This method ensures all related data is properly cleaned up, including:
        - Invitation tokens for the user
        - Any other user-related data
        
        Args:
            user_id (str): The ID of the user to delete
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not user_id:
            logger.warning("Attempted to delete user with empty user_id")
            return False
            
        try:
            # Check if user exists before attempting to delete
            user = self.get_user_by_id(user_id)
            if not user:
                logger.warning(f"Attempted to delete non-existent user: {user_id}")
                return False
                
            # Delete invitation tokens first (maintain referential integrity)
            self._execute_query("DELETE FROM invitation_tokens WHERE user_id = ?", (user_id,))
            logger.info(f"Deleted invitation tokens for user: {user_id}")
            
            # Delete the user
            query = "DELETE FROM users WHERE user_id = ?"
            self._execute_query(query, (user_id,))
            logger.info(f"Deleted user: {user_id} ({user.username})")
            
            return True
        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}")
            # Ensure we don't crash the application
            return False
    
    def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get a user by their ID with robust error handling"""
        if not user_id:
            logger.warning("Empty user_id provided to get_user_by_id")
            return None
        
        try:
            query = "SELECT * FROM users WHERE user_id = ?"
            result = self._execute_query(query, (user_id,))
            if result is None:
                logger.error("Query execution returned None in get_user_by_id")
                return None
            
            row = result.fetchone()
            if not row:
                logger.debug(f"No user found with id: {user_id}")
                return None
            
            # Get column names to ensure proper mapping regardless of schema changes
            columns = [col[0] for col in result.description]
            user_data = {columns[i]: row[i] for i in range(len(columns))}
            
            # Create User object with proper mapping
            return User(
                id=user_data.get('user_id'),
                username=user_data.get('username'),
                email=user_data.get('email'),
                password_hash=user_data.get('password'),
                role=user_data.get('role'),
                created_at=user_data.get('created_at'),
                last_login=user_data.get('last_login'),
                is_active=bool(user_data.get('is_active', 1)),
                invite_token=user_data.get('invite_token'),
                token_expiry=user_data.get('token_expiry')
            )
        except Exception as e:
            logger.error(f"Error in get_user_by_id: {e}")
            return None
    
    def get_user_by_email(self, email):
        """Get a user by their email
        
        Args:
            email (str): The email to look up
            
        Returns:
            dict: The user data or None if not found
        """
        try:
            query = "SELECT * FROM users WHERE email = ?"
            result = self._execute_query(query, (email,))
            user_data = result.fetchone()
            
            if not user_data:
                return None
                
            # Convert to dictionary with column names
            columns = [col[0] for col in result.description]
            user_dict = {columns[i]: user_data[i] for i in range(len(columns))}
            
            return user_dict
        except Exception as e:
            logger.error(f"Error getting user by email: {e}")
            return None
    
    def get_user_by_username(self, username):
        """Get a user by their username
        
        Args:
            username (str): The username to look up
            
        Returns:
            dict: The user data or None if not found
        """
        try:
            query = "SELECT * FROM users WHERE username = ?"
            result = self._execute_query(query, (username,))
            user_data = result.fetchone()
            
            if not user_data:
                return None
                
            # Convert to dictionary with column names
            columns = [col[0] for col in result.description]
            user_dict = {columns[i]: user_data[i] for i in range(len(columns))}
            
            return user_dict
        except Exception as e:
            logger.error(f"Error getting user by username: {e}")
            return None
    
    def get_user_by_invite_token(self, token: str) -> Optional[User]:
        """Get a user by their invite token with robust error handling"""
        if not token:
            logger.warning("Empty token provided to get_user_by_invite_token")
            return None
        
        try:
            query = "SELECT * FROM users WHERE invite_token = ?"
            result = self._execute_query(query, (token,))
            if result is None:
                logger.error("Query execution returned None in get_user_by_invite_token")
                return None
            
            row = result.fetchone()
            if not row:
                logger.debug(f"No user found with invite token: {token}")
                return None
            
            # Get column names to ensure proper mapping regardless of schema changes
            columns = [col[0] for col in result.description]
            user_data = {columns[i]: row[i] for i in range(len(columns))}
            
            # Create User object with proper mapping
            return User(
                id=user_data.get('user_id'),
                username=user_data.get('username'),
                email=user_data.get('email'),
                password_hash=user_data.get('password'),
                role=user_data.get('role'),
                created_at=user_data.get('created_at'),
                last_login=user_data.get('last_login'),
                is_active=bool(user_data.get('is_active', 1)),
                invite_token=user_data.get('invite_token'),
                token_expiry=user_data.get('token_expiry')
            )
        except Exception as e:
            logger.error(f"Error in get_user_by_invite_token: {e}")
            return None
    
    def get_all_users(self) -> List[User]:
        """Get all users from the database"""
        query = "SELECT * FROM users ORDER BY created_at DESC"
        result = self._execute_query(query)
        users = []
        
        # Get column names to ensure proper mapping regardless of schema changes
        if result:
            columns = [col[0] for col in result.description]
            
            for row in result:
                # Create dictionary mapping column names to values
                user_data = {columns[i]: row[i] for i in range(len(columns))}
                
                # Create User object with proper mapping
                users.append(User(
                    id=user_data.get('user_id'),
                    username=user_data.get('username'),
                    email=user_data.get('email'),
                    password_hash=user_data.get('password'),
                    role=user_data.get('role'),
                    created_at=user_data.get('created_at'),
                    last_login=user_data.get('last_login'),
                    is_active=bool(user_data.get('is_active', 1)),
                    invite_token=user_data.get('invite_token'),
                    token_expiry=user_data.get('token_expiry')
                ))
        
        return users
    
    def update_last_login(self, user_id: str):
        """Update a user's last login time"""
        query = "UPDATE users SET last_login = ? WHERE user_id = ?"
        self._execute_query(query, (int(time.time()), user_id))
    
    def create_invite_token(self, user_id: str) -> str:
        """Create an invite token for a user"""
        # Generate a random token
        token = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        # Set expiry to 7 days from now
        expiry = int(time.time()) + (7 * 24 * 60 * 60)
        
        query = "UPDATE users SET invite_token = ?, token_expiry = ? WHERE id = ?"
        self._execute_query(query, (token, expiry, user_id))
        
        return token

    def update_view_name(self, new_name: str, view_id: str, screen_id: str):
        """Update the name of a view.
        
        Args:
            new_name (str): The new name for the view
            view_id (str): The ID of the view to update
            screen_id (str): The ID of the screen the view belongs to
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            query = """
            UPDATE views 
            SET name = ?
            WHERE id = ? AND screen_id = ?
            """
            self._execute_query(query, (new_name, view_id, screen_id))
            logger.info(f"Updated view name: {view_id} -> {new_name}")
            return True
        except Exception as e:
            logger.error(f"Error updating view name: {e}")
            return False

    def _execute_query(self, query, params=None):
        """
        Execute a database query with proper error handling.
        Creates a new connection for each query to avoid thread safety issues.
        
        Args:
            query (str): The SQL query to execute
            params (tuple, optional): Parameters for the query
            
        Returns:
            sqlite3.Cursor or NullCursor: A cursor object for the query results or NullCursor on error
        """
        conn = None
        try:
            # Create a new connection for this query
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
                
            conn.commit()
            
            # Create a wrapper to hold the results and close the connection properly
            class CursorWrapper:
                def __init__(self, cursor, connection):
                    self.cursor = cursor
                    self.connection = connection
                    self.description = cursor.description
                
                def fetchone(self):
                    result = self.cursor.fetchone()
                    return result
                
                def fetchall(self):
                    result = self.cursor.fetchall()
                    return result
                
                def __iter__(self):
                    return iter(self.cursor)
                
                def close(self):
                    self.connection.close()
            
            return CursorWrapper(cursor, conn)
        except sqlite3.Error as e:
            logger.error(f"Database query error: {e} - Query: {query}")
            if conn:
                conn.rollback()
                conn.close()
            return NullCursor()
        except Exception as e:
            logger.error(f"Unexpected error in database query: {e} - Query: {query}")
            if conn:
                conn.rollback()
                conn.close()
            return NullCursor()

    def get_view_config(
        self, pc_id: str, screen_id: str, view_id: str
    ) -> Dict[str, Any]:
        """
        Get the configuration for a specific view including all necessary fields.
        
        Args:
            pc_id (str): The PC ID
            screen_id (str): The screen ID
            view_id (str): The view ID
            
        Returns:
            Dict[str, Any]: View configuration with slots and camera details
        """
        if not all([pc_id, screen_id, view_id]):
            logger.warning("Missing required parameters for get_view_config")
            return {}

        try:
            query = """
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
            """
            
            result = self._execute_query(query, (pc_id, screen_id, view_id))
            if not result:
                logger.error("Failed to execute query in get_view_config")
                return {}
                
            rows = result.fetchall()
            view_config = {}

            for row in rows:
                if len(row) < 8:  # Ensure we have all expected columns
                    logger.warning(f"Incomplete row data in get_view_config: {row}")
                    continue
                    
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
                    "site_name": site_name or "Unknown Site",
                    "camera_name": camera_name or "Unknown Camera",
                    "rtsp_url": rtsp_url or "",
                }

            return view_config

        except Exception as e:
            logger.error(f"Error in get_view_config: {e}")
            return {}
        
    def get_pc_config(self, pc_id: str) -> Dict[str, Any]:
        """
        Get the complete configuration for a PC including all screens and views
        in the format expected by generate_config().
        
        Args:
            pc_id (str): The ID of the PC to get configuration for
            
        Returns:
            Dict[str, Any]: Complete PC configuration with screens, views and mappings
        """
        if not pc_id:
            logger.warning("Empty pc_id provided to get_pc_config")
            return {}

        try:
            # Get PC details
            pc_query = "SELECT id, name FROM pcs WHERE id = ?"
            pc_result = self._execute_query(pc_query, (pc_id,))
            if not pc_result:
                logger.error("Failed to execute PC query in get_pc_config")
                return {}
                
            pc_row = pc_result.fetchone()
            if not pc_row:
                logger.warning(f"No PC found with id: {pc_id}")
                return {}
                
            pc_id, pc_name = pc_row
            
            # Get all screens for this PC
            screens_query = "SELECT id, name, rows, columns, switching_interval FROM screens WHERE pc_id = ?"
            screens_result = self._execute_query(screens_query, (pc_id,))
            if not screens_result:
                logger.error("Failed to execute screens query in get_pc_config")
                return {}
                
            screens = screens_result.fetchall()
            
            # Initialize result structure
            result = {
                "pcs": {
                    pc_id: {
                        "name": pc_name,
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
                screen_id, screen_name, rows, columns, switching_interval = screen
                
                # Add screen info
                result["pcs"][pc_id]["screens"][screen_id] = {
                    "name": screen_name,
                    "layout": {
                        "rows": rows,
                        "columns": columns
                    },
                    "switching_interval": switching_interval  
                }
                
                # Get all views for this screen
                views_query = "SELECT id, name, view_number FROM views WHERE screen_id = ? ORDER BY view_number"
                views_result = self._execute_query(views_query, (screen_id,))
                if not views_result:
                    logger.warning(f"Failed to get views for screen: {screen_id}")
                    continue
                    
                views = views_result.fetchall()
                
                # Initialize screen mappings
                result["mappings"]["screen_to_cameras"][pc_id][screen_id] = {}
                
                # For each view, get the slot mappings
                for view in views:
                    view_id, view_name, view_number = view
                    
                    # Get all slot mappings for this view
                    mappings_query = """
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
                    """
                    
                    mappings_result = self._execute_query(mappings_query, (pc_id, screen_id, view_id))
                    if not mappings_result:
                        logger.warning(f"Failed to get mappings for view: {view_id}")
                        continue
                        
                    mappings = mappings_result.fetchall()
                    
                    # Store the view configuration
                    view_config = {}
                    for mapping in mappings:
                        if len(mapping) < 7:  # Ensure we have all expected columns
                            logger.warning(f"Incomplete mapping data: {mapping}")
                            continue
                            
                        slot_row, slot_col, site_id, camera_id, site_name, camera_name, rtsp_url = mapping
                        slot_key = f"slot_{slot_row}_{slot_col}"
                        view_config[slot_key] = {
                            "site_id": site_id,
                            "camera_id": camera_id,
                            "site_name": site_name or "Unknown Site",
                            "camera_name": camera_name or "Unknown Camera",
                            "rtsp_url": rtsp_url or ""
                        }
                    
                    # Only add the view if it has mappings
                    if view_config:
                        result["mappings"]["screen_to_cameras"][pc_id][screen_id][view_name] = view_config
            
            return result
                
        except Exception as e:
            logger.error(f"Error in get_pc_config: {e}")
            return {}

    def get_view_by_id(self, view_id: str) -> Optional[View]:
        """Get a view by its ID
        
        Args:
            view_id (str): The ID of the view to get
            
        Returns:
            Optional[View]: The View object if found, None otherwise
        """
        if not view_id:
            logger.warning("Empty view_id provided to get_view_by_id")
            return None
            
        try:
            query = """
            SELECT id, screen_id, name, layout_rows, layout_columns, view_number 
            FROM views WHERE id = ?
            """
            result = self._execute_query(query, (view_id,))
            
            if result is None:
                return None
                
            row = result.fetchone()
            if not row:
                return None
                
            return View(*row)
        except Exception as e:
            logger.error(f"Error in get_view_by_id: {e}")
            return None

    def update_view(self, view: View) -> bool:
        """Update an existing view
        
        Args:
            view (View): The view object with updated values
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not view or not view.id:
            logger.warning("Invalid view provided to update_view")
            return False
            
        try:
            query = """
            UPDATE views
            SET screen_id = ?, name = ?, layout_rows = ?, layout_columns = ?
            WHERE id = ?
            """
            self._execute_query(
                query, 
                (view.screen_id, view.name, view.layout_rows, view.layout_columns, view.id)
            )
            logger.info(f"Updated view: {view.id}")
            return True
        except Exception as e:
            logger.error(f"Error updating view: {e}")
            return False

    def delete_view(self, view_id: str) -> bool:
        """Delete a view by its ID
        
        Args:
            view_id (str): The ID of the view to delete
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not view_id:
            logger.warning("Empty view_id provided to delete_view")
            return False
            
        try:
            # First delete any screen mappings for this view
            self._execute_query("DELETE FROM screen_mappings WHERE view_id = ?", (view_id,))
            
            # Then delete the view
            self._execute_query("DELETE FROM views WHERE id = ?", (view_id,))
            
            logger.info(f"Deleted view: {view_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting view: {e}")
            return False

    def add_view(self, view: View) -> bool:
        """Add a new view
        
        Args:
            view (View): The view object to add
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not view or not view.id:
            logger.warning("Invalid view provided to add_view")
            return False
            
        try:
            query = """
            INSERT INTO views (id, screen_id, name, layout_rows, layout_columns, view_number)
            VALUES (?, ?, ?, ?, ?, ?)
            """
            self._execute_query(
                query,
                (view.id, view.screen_id, view.name, view.layout_rows, view.layout_columns, view.view_number)
            )
            
            logger.info(f"Added view: {view.name} (ID: {view.id})")
            return True
        except Exception as e:
            logger.error(f"Error adding view: {e}")
            return False

    def get_views_by_screen(self, screen_id: str) -> List[View]:
        """Get all views for a screen
        
        Args:
            screen_id (str): The ID of the screen to get views for
            
        Returns:
            List[View]: List of View objects for the screen
        """
        if not screen_id:
            logger.warning("Empty screen_id provided to get_views_by_screen")
            return []
            
        try:
            query = """
            SELECT id, screen_id, name, layout_rows, layout_columns, view_number 
            FROM views 
            WHERE screen_id = ?
            """
            result = self._execute_query(query, (screen_id,))
            
            views = []
            if result:
                for row in result:
                    views.append(View(*row))
                    
            return views
        except Exception as e:
            logger.error(f"Error in get_views_by_screen: {e}")
            return []

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

    def get_screen_by_id(self, screen_id: str) -> Optional[Screen]:
        """
        Get a screen by its ID
        
        Args:
            screen_id (str): The ID of the screen to get
            
        Returns:
            Optional[Screen]: The Screen object if found, None otherwise
        """
        if not screen_id:
            logger.warning("Empty screen_id provided to get_screen_by_id")
            return None
            
        try:
            query = """
            SELECT id, pc_id, name, rows, columns, switching_interval 
            FROM screens WHERE id = ?
            """
            
            result = self._execute_query(query, (screen_id,))
            if not result:
                logger.error("Failed to execute query in get_screen_by_id")
                return None
                
            row = result.fetchone()
            if not row:
                logger.debug(f"No screen found with id: {screen_id}")
                return None
                
            return Screen(*row)
        except Exception as e:
            logger.error(f"Error in get_screen_by_id: {e}")
            return None

    def get_pc_by_id(self, pc_id: str) -> Optional[PC]:
        """Get a PC by its ID
        
        Args:
            pc_id (str): The ID of the PC to get
            
        Returns:
            Optional[PC]: The PC object if found, None otherwise
        """
        if not pc_id:
            logger.warning("Empty pc_id provided to get_pc_by_id")
            return None
            
        try:
            query = """
            SELECT id, name, ip_address, gpu_type, role, manager_id, auth_token, token_expiry, last_connected, last_applied 
            FROM pcs WHERE id = ?
            """
            result = self._execute_query(query, (pc_id,))
            
            if result is None:
                return None
                
            row = result.fetchone()
            if not row:
                return None
                
            return PC(*row)
        except Exception as e:
            logger.error(f"Error in get_pc_by_id: {e}")
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
        """
        Get all screens for a specific PC
        
        Args:
            pc_id (str): The ID of the PC to get screens for
            
        Returns:
            List[Screen]: List of Screen objects for the PC
        """
        if not pc_id:
            logger.warning("Empty pc_id provided to get_screens_by_pc")
            return []
            
        try:
            query = """
            SELECT id, pc_id, name, rows, columns, switching_interval
            FROM screens
            WHERE pc_id = ?
            """
            
            result = self._execute_query(query, (pc_id,))
            if not result:
                logger.error("Failed to execute query in get_screens_by_pc")
                return []
                
            screens = []
            for row in result:
                if len(row) >= 6:  # Ensure we have all required columns
                    screens.append(Screen(*row))
                else:
                    logger.warning(f"Incomplete screen data: {row}")
                    
            return screens
        except Exception as e:
            logger.error(f"Error in get_screens_by_pc: {e}")
            return []

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

    def add_screen_mapping(self, mapping: ScreenMapping) -> bool:
        """
        Add or update a screen mapping
        
        Args:
            mapping (ScreenMapping): The screen mapping to add/update
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not mapping or not all([mapping.screen_id, mapping.view_id, mapping.site_id, mapping.camera_id]):
            logger.warning("Invalid mapping provided to add_screen_mapping")
            return False
            
        try:
            # Check if the mapping already exists
            check_query = """
            SELECT 1 FROM screen_mappings
            WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
            """
            
            result = self._execute_query(check_query, (
                mapping.screen_id,
                mapping.view_id,
                mapping.slot_row,
                mapping.slot_col
            ))
            
            if not result:
                logger.error("Failed to execute check query in add_screen_mapping")
                return False
                
            existing_mapping = result.fetchone()

            if existing_mapping:
                # Update the existing mapping
                update_query = """
                UPDATE screen_mappings
                SET site_id = ?, camera_id = ?, pc_id = ?
                WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
                """
                
                self._execute_query(update_query, (
                    mapping.site_id,
                    mapping.camera_id,
                    mapping.pc_id,
                    mapping.screen_id,
                    mapping.view_id,
                    mapping.slot_row,
                    mapping.slot_col
                ))
                
                logger.info(f"Updated screen mapping: {mapping.screen_id}, view: {mapping.view_id}, slot: {mapping.slot_row},{mapping.slot_col}")
            else:
                # Insert a new mapping
                insert_query = """
                INSERT INTO screen_mappings (pc_id, screen_id, view_id, slot_row, slot_col, site_id, camera_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """
                
                self._execute_query(insert_query, (
                    mapping.pc_id,
                    mapping.screen_id,
                    mapping.view_id,
                    mapping.slot_row,
                    mapping.slot_col,
                    mapping.site_id,
                    mapping.camera_id
                ))
                
                logger.info(f"Added new screen mapping: {mapping.screen_id}, view: {mapping.view_id}, slot: {mapping.slot_row},{mapping.slot_col}")
                
            return True
        except Exception as e:
            logger.error(f"Error in add_screen_mapping: {e}")
            return False

    def delete_screen_mapping(
        self, screen_id: str, view_id: str, slot_row: int, slot_col: int
    ) -> bool:
        """
        Delete a screen mapping
        
        Args:
            screen_id (str): The screen ID
            view_id (str): The view ID
            slot_row (int): The slot row
            slot_col (int): The slot column
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not all([screen_id, view_id, isinstance(slot_row, int), isinstance(slot_col, int)]):
            logger.warning("Invalid parameters provided to delete_screen_mapping")
            return False
            
        try:
            query = """
            DELETE FROM screen_mappings
            WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
            """
            
            self._execute_query(query, (screen_id, view_id, slot_row, slot_col))
            logger.info(f"Deleted screen mapping: {screen_id}, view: {view_id}, slot: {slot_row},{slot_col}")
            return True
        except Exception as e:
            logger.error(f"Error in delete_screen_mapping: {e}")
            return False

    def update_screen_mapping(self, mapping: ScreenMapping) -> bool:
        """
        Update a screen mapping
        
        Args:
            mapping (ScreenMapping): The updated screen mapping
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not mapping or not all([mapping.screen_id, mapping.view_id, mapping.site_id, mapping.camera_id]):
            logger.warning("Invalid mapping provided to update_screen_mapping")
            return False
            
        try:
            query = """
            UPDATE screen_mappings
            SET site_id = ?, camera_id = ?
            WHERE screen_id = ? AND view_id = ? AND slot_row = ? AND slot_col = ?
            """
            
            self._execute_query(query, (
                mapping.site_id,
                mapping.camera_id,
                mapping.screen_id,
                mapping.view_id,
                mapping.slot_row,
                mapping.slot_col
            ))
            
            logger.info(f"Updated screen mapping: {mapping.screen_id}, view: {mapping.view_id}, slot: {mapping.slot_row},{mapping.slot_col}")
            return True
        except Exception as e:
            logger.error(f"Error in update_screen_mapping: {e}")
            return False

    def get_screen_mappings(self, screen_id: str, view_id: str) -> List[ScreenMapping]:
        """
        Get all screen mappings for a specific screen and view
        
        Args:
            screen_id (str): The screen ID
            view_id (str): The view ID
            
        Returns:
            List[ScreenMapping]: List of ScreenMapping objects
        """
        if not screen_id or not view_id:
            logger.warning("Missing required parameters for get_screen_mappings")
            return []
            
        try:
            query = """
            SELECT screen_id, view_id, slot_row, slot_col, site_id, camera_id, playing_state
            FROM screen_mappings
            WHERE screen_id = ? AND view_id = ?
            """
            
            result = self._execute_query(query, (screen_id, view_id))
            if not result:
                logger.error("Failed to execute query in get_screen_mappings")
                return []
                
            mappings = []
            for row in result:
                if len(row) >= 7:  # Ensure we have all required columns
                    mappings.append(ScreenMapping(*row))
                else:
                    logger.warning(f"Incomplete mapping data: {row}")
                    
            return mappings
        except Exception as e:
            logger.error(f"Error in get_screen_mappings: {e}")
            return []

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

    def generate_invitation_token(self, user_id, expiration_days=7):
        """
        Generate an invitation token for a user
        
        Args:
            user_id (str): The ID of the user
            expiration_days (int, optional): Number of days until token expires. Defaults to 7.
            
        Returns:
            str: The generated token if successful, None otherwise
        """
        try:
            # Generate a unique token
            token = secrets.token_urlsafe(32)
            
            # Set expiration time
            current_time = int(time.time())
            expires_at = current_time + (expiration_days * 24 * 60 * 60)
            
            # Check if this user already has tokens
            check_query = "SELECT token FROM invitation_tokens WHERE user_id = ? AND is_used = 0"
            result = self._execute_query(check_query, (user_id,))
            existing_token = result.fetchone()
            
            if existing_token:
                # Return the existing active token
                logger.info(f"Returning existing invitation token for user {user_id}")
                return existing_token[0]
                
            # Insert token
            insert_query = """
                INSERT INTO invitation_tokens (token, user_id, created_at, expires_at, is_used)
                VALUES (?, ?, ?, ?, ?)
            """
            self._execute_query(insert_query, (token, user_id, current_time, expires_at, 0))
            
            logger.info(f"Generated new invitation token for user {user_id}")
            return token
        except Exception as e:
            logger.error(f"Error in generate_invitation_token: {e}")
            # Return None instead of raising, for better user experience
            return None
    
    def get_user_by_token(self, token):
        """
        Get a user by invitation token
        
        Args:
            token (str): The invitation token
            
        Returns:
            User: The user object if found and token is valid, None otherwise
        """
        try:
            if not token:
                logger.warning("Empty token provided to get_user_by_token")
                return None
                
            # Get token and check if it's valid
            token_query = "SELECT user_id, expires_at, is_used FROM invitation_tokens WHERE token = ?"
            token_result = self._execute_query(token_query, (token,))
            token_data = token_result.fetchone()
            
            if not token_data:
                logger.warning(f"Token not found: {token}")
                return None
            
            user_id, expires_at, is_used = token_data
            
            # Check if token is expired
            if int(time.time()) > int(expires_at):
                logger.warning(f"Token expired: {token}")
                return None
            
            # Check if token has been used
            if is_used:
                logger.warning(f"Token already used: {token}")
                return None
            
            # Get the user
            return self.get_user_by_id(user_id)
        except Exception as e:
            logger.error(f"Error in get_user_by_token: {e}")
            return None
            
    def reset_password(self, user_id: str, new_password: str) -> bool:
        """
        Reset a user's password
        
        Args:
            user_id (str): The ID of the user
            new_password (str): The new password
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Hash the new password
            password_hash = self._hash_password(new_password)
            
            # Update password
            query = "UPDATE users SET password = ? WHERE user_id = ?"
            self._execute_query(query, (password_hash, user_id))
            
            logger.info(f"Reset password for user: {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error resetting password: {e}")
            return False

    def create_default_user(self):
        """Create a default super admin user if no users exist"""
        try:
            # Check if users table exists and has any rows
            count = self._execute_query("SELECT COUNT(*) FROM users").fetchone()[0]
            
            if count == 0:
                # No users exist, create default super admin
                username = "admin"
                email = "admin@example.com"
                password = "admin123"
                role = "super_admin"
                
                # Hash password with salt
                hashed_password = self._hash_password(password)
                
                # Generate user ID
                user_id = str(uuid.uuid4())
                
                # Current time
                created_at = datetime.now().isoformat()
                
                # Execute query to insert default user
                query = """
                INSERT INTO users (user_id, username, email, password, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """
                
                self._execute_query(
                    query, 
                    (user_id, username, email, hashed_password, role, 1, created_at)
                )
                
                logger.info(f"Created default super admin user: {username} (id: {user_id})")
                return True
            return False
        except Exception as e:
            logger.error(f"Error creating default user: {e}")
            return False

    def update_user_last_login(self, user_id):
        """Update the last login time for a user
        
        Args:
            user_id (str): The ID of the user to update
            
        Returns:
            bool: Whether the update was successful
        """
        try:
            # Get current time
            last_login = datetime.now().isoformat()
            
            # Update user's last login time
            query = "UPDATE users SET last_login = ? WHERE user_id = ?"
            self._execute_query(query, (last_login, user_id))
            
            logger.info(f"Updated last login time for user: {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error updating last login time: {e}")
            return False

    def consume_invitation_token(self, token):
        """
        Mark an invitation token as used
        
        Args:
            token (str): The token to mark as used
            
        Returns:
            bool: True if token was consumed successfully, False otherwise
        """
        if not token:
            logger.warning("Attempted to consume empty invitation token")
            return False
            
        try:
            # Mark token as used using the _execute_query helper method
            query = "UPDATE invitation_tokens SET is_used = 1 WHERE token = ?"
            self._execute_query(query, (token,))
            
            logger.info(f"Token marked as used: {token}")
            return True
        except Exception as e:
            logger.error(f"Error in consume_invitation_token: {e}")
            return False
    
    def check_super_admin_exists(self):
        """
        Check if at least one super_admin user exists
        
        Returns:
            bool: True if at least one super_admin exists, False otherwise
        """
        try:
            query = "SELECT COUNT(*) FROM users WHERE role = 'super_admin'"
            result = self._execute_query(query)
            count = result.fetchone()[0]
            
            return count > 0
        except Exception as e:
            logger.error(f"Error in check_super_admin_exists: {e}")
            # In case there's an issue with the table doesn't exist yet
            # This likely means we're in first-time setup, so return False
            return False

    def create_user(self, username: str, email: str, role: str, password: str = None, is_active: bool = True) -> Optional[str]:
        """
        Create a new user with optional password
        
        Args:
            username (str): The username for the new user
            email (str): The email address for the new user
            role (str): The role for the new user
            password (str, optional): The password for the new user. If not provided, user will need to set it via invite
            is_active (bool): Whether the user is active
            
        Returns:
            Optional[str]: The user ID if successful, None otherwise
        """
        try:
            # Check if username exists
            existing_user = self.get_user_by_username(username)
            if existing_user:
                logger.warning(f"Username already exists: {username}")
                return None
            
            # Generate user ID
            user_id = str(uuid.uuid4())
            
            # Hash password if provided
            # We require a password due to NOT NULL constraint, so use a temporary one if none provided
            if not password:
                password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
                logger.info(f"Generated temporary password for user {username}")
                
            password_hash = self._hash_password(password)
            
            # Insert user
            query = """
                INSERT INTO users (
                    user_id, username, email, password, role, 
                    created_at, last_login, is_active, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            
            self._execute_query(
                query,
                (
                    user_id,
                    username,
                    email,
                    password_hash,
                    role,
                    int(time.time()),
                    None,
                    1 if is_active else 0,
                    None  # created_by is None for initial creation
                )
            )
            
            logger.info(f"Created new user: {username} (role: {role})")
            return user_id
            
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return None

    def add_or_update_screenshot(self, camera_id: str, image) -> bool:
        """
        Add or update a screenshot for a camera
        
        Args:
            camera_id (str): The ID of the camera
            image: OpenCV (cv2) image object
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            import cv2
            import numpy as np
            
            if not camera_id or image is None:
                logger.warning("Invalid camera_id or image provided to add_or_update_screenshot")
                return False
            
            # Get image dimensions
            height, width = image.shape[:2]
            
            # Convert image to bytes
            _, img_encoded = cv2.imencode('.jpg', image)
            img_bytes = img_encoded.tobytes()
            
            # Current time as Unix timestamp
            capture_time = int(time.time())
            
            # Check if a screenshot already exists for this camera
            check_query = "SELECT 1 FROM screenshots WHERE camera_id = ?"
            result = self._execute_query(check_query, (camera_id,))
            exists = result.fetchone() is not None
            
            if exists:
                # Update existing screenshot
                update_query = """
                UPDATE screenshots
                SET image = ?, height = ?, width = ?, capture_time = ?
                WHERE camera_id = ?
                """
                self._execute_query(update_query, (img_bytes, height, width, capture_time, camera_id))
                logger.info(f"Updated screenshot for camera {camera_id}")
            else:
                # Insert new screenshot
                insert_query = """
                INSERT INTO screenshots (camera_id, image, height, width, capture_time)
                VALUES (?, ?, ?, ?, ?)
                """
                self._execute_query(insert_query, (camera_id, img_bytes, height, width, capture_time))
                logger.info(f"Added screenshot for camera {camera_id}")
            
            return True
        except Exception as e:
            logger.error(f"Error in add_or_update_screenshot: {e}")
            return False

    def get_screenshot(self, camera_id: str):
        """
        Get a screenshot for a camera
        
        Args:
            camera_id (str): The ID of the camera
            
        Returns:
            image: OpenCV (cv2) image object if found, None otherwise
        """
        try:
            import cv2
            import numpy as np
            
            if not camera_id:
                logger.warning("Empty camera_id provided to get_screenshot")
                return None
            
            query = "SELECT image FROM screenshots WHERE camera_id = ?"
            result = self._execute_query(query, (camera_id,))
            
            if not result:
                logger.error("Failed to execute query in get_screenshot")
                return None
            
            row = result.fetchone()
            if not row:
                logger.debug(f"No screenshot found for camera {camera_id}")
                return None
            
            # Convert bytes to image
            img_bytes = row[0]
            img_array = np.frombuffer(img_bytes, dtype=np.uint8)
            image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            
            return image
        except Exception as e:
            logger.error(f"Error in get_screenshot: {e}")
            return None

def get_streaming_data():
    """
    Get all streaming data
    
    Returns:
        pandas.DataFrame: DataFrame with streaming data
    """
    try:
        db = Database()
        # Use connection properly
        conn = sqlite3.connect(db.db_path)
        
        try:
            query = "SELECT * FROM streaming_data"
            df = pd.read_sql_query(query, conn)
            
            # Convert date from Unix timestamp to datetime
            if not df.empty:
                df['date'] = pd.to_datetime(df['date'], unit='s')
            
            return df
        finally:
            # Ensure connection is closed
            if conn:
                conn.close()
    except Exception as e:
        logger.error(f"Error in get_streaming_data: {e}")
        # Return empty DataFrame on error
        return pd.DataFrame()
