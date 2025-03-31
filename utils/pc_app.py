import socketio
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox, scrolledtext
import threading
import uuid
import time
import jwt
import json
import os
import logging
import websockets
import asyncio
from io import StringIO
import sys
import traceback
from pystray import MenuItem as item
import pystray
from PIL import Image
import hashlib
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import sqlite3

# Set up logging with file rotation
try:
    import logging.handlers

    log_dir = os.path.join(os.path.expanduser("~"), ".Shomer_Client", "logs")
    os.makedirs(log_dir, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "Shomer_Client.log"),
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
    )
    console_handler = logging.StreamHandler()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[file_handler, console_handler],
    )
except Exception as e:
    # Fallback to basic logging if setting up advanced logging fails
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logging.error(f"Failed to set up advanced logging: {e}")

logger = logging.getLogger("Shomer_Client")

# Default configuration
DEFAULT_SERVER_URL = "http://18.204.201.19:8080"
DEFAULT_LOCAL_WS_PORT = "9022"

# For production, disable auto token generation
AUTO_GENERATE_TOKEN = False

# Application data directory
APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".Shomer_Client")
SETTINGS_DB_PATH = os.path.join(APP_DATA_DIR, "settings.db")
SALT_FILE = os.path.join(APP_DATA_DIR, "salt.bin")


class SettingsManager:
    def __init__(self):
        self.settings = {
            "server_url": DEFAULT_SERVER_URL,
            "local_ws_port": DEFAULT_LOCAL_WS_PORT,
            "first_run": True,
        }
        self.init_settings_db()

    def init_settings_db(self):
        """Initialize the settings database"""
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)

            # Create or open the database
            conn = sqlite3.connect(SETTINGS_DB_PATH)
            cursor = conn.cursor()

            # Create settings table if it doesn't exist
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                encrypted INTEGER DEFAULT 0
            )
            """
            )

            # Create password hash table
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS security (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
            )

            conn.commit()
            conn.close()
            logger.info("Settings database initialized")
        except Exception as e:
            logger.error(f"Error initializing settings database: {e}")
            raise

    def load_settings(self):
        """Load settings from the database"""
        try:
            conn = sqlite3.connect(SETTINGS_DB_PATH)
            cursor = conn.cursor()

            # Check if there are any settings
            cursor.execute("SELECT COUNT(*) FROM settings")
            count = cursor.fetchone()[0]

            if count == 0:
                # First run detected
                self.settings["first_run"] = True
                conn.close()
                return self.settings

            # Load all non-encrypted settings
            cursor.execute("SELECT key, value FROM settings WHERE encrypted = 0")
            for key, value in cursor.fetchall():
                self.settings[key] = value

            # Set first run flag to False since we found settings
            self.settings["first_run"] = False

            conn.close()
            return self.settings
            except Exception as e:
            logger.error(f"Error loading settings: {e}")
            return self.settings

    def save_setting(self, key, value, encrypted=False):
        """Save a setting to the database"""
        try:
            conn = sqlite3.connect(SETTINGS_DB_PATH)
            cursor = conn.cursor()

            # Update or insert setting
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value, encrypted) VALUES (?, ?, ?)",
                (key, value, 1 if encrypted else 0),
            )

            conn.commit()
            conn.close()

            # Update in-memory settings
            self.settings[key] = value
            logger.info(f"Setting saved: {key}")
        except Exception as e:
            logger.error(f"Error saving setting {key}: {e}")

    def get_setting(self, key, default=None):
        """Get a setting value, with fallback to default"""
        return self.settings.get(key, default)

    def check_password(self, password):
        """Check if the provided password matches the stored hash"""
        try:
            conn = sqlite3.connect(SETTINGS_DB_PATH)
            cursor = conn.cursor()

            cursor.execute("SELECT value FROM security WHERE key = 'password_hash'")
            result = cursor.fetchone()
            conn.close()

            if not result:
                return False

            stored_hash = result[0]
            password_hash = self._hash_password(password)

            return stored_hash == password_hash
        except Exception as e:
            logger.error(f"Error checking password: {e}")
            return False

    def set_password(self, password):
        """Set the admin password"""
        try:
            password_hash = self._hash_password(password)

            conn = sqlite3.connect(SETTINGS_DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                "INSERT OR REPLACE INTO security (key, value) VALUES (?, ?)",
                ("password_hash", password_hash),
            )

            # Generate and store encryption salt
            if not os.path.exists(SALT_FILE):
                salt = os.urandom(16)
                with open(SALT_FILE, "wb") as f:
                    f.write(salt)

            conn.commit()
            conn.close()
            logger.info("Password set successfully")
            return True
        except Exception as e:
            logger.error(f"Error setting password: {e}")
            return False
    
    def _hash_password(self, password):
        """Create a secure hash of the password"""
        return hashlib.sha256(password.encode()).hexdigest()

    def get_password_hash(self):
        """Get the stored password hash if any"""
        try:
            conn = sqlite3.connect(SETTINGS_DB_PATH)
            cursor = conn.cursor()

            cursor.execute("SELECT value FROM security WHERE key = 'password_hash'")
            result = cursor.fetchone()
            conn.close()

            return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting password hash: {e}")
            return None

    def encrypt_value(self, password, value):
        """Encrypt a value using the password-derived key"""
        try:
            # Load or create salt
            if os.path.exists(SALT_FILE):
                with open(SALT_FILE, "rb") as f:
                    salt = f.read()
            else:
                salt = os.urandom(16)
                with open(SALT_FILE, "wb") as f:
                    f.write(salt)

            # Create key from password
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))

            # Encrypt the value
            f = Fernet(key)
            return f.encrypt(value.encode()).decode()
        except Exception as e:
            logger.error(f"Encryption error: {e}")
            return None

    def decrypt_value(self, password, encrypted_value):
        """Decrypt a value using the password-derived key"""
        try:
            # Load salt
            with open(SALT_FILE, "rb") as f:
                salt = f.read()

            # Create key from password
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))

            # Decrypt the value
            f = Fernet(key)
            return f.decrypt(encrypted_value.encode()).decode()
        except Exception as e:
            logger.error(f"Decryption error: {e}")
            return None


class ModernClient:
    def __init__(self, root):
        self.root = root
        self.sio = None  # Will be initialized later
        self.connected = False
        self.debug_visible = False  # Hide debug by default for production
        self.current_config = None
        self.pc_info = {"name": "", "role": "", "id": ""}
        self.config_history = []
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.current_token = None
        self.reconnect_timer = None
        self.exit_flag = False
        self.connection_thread = None
        self.minimized_to_tray = False
        self.icon = None  # Will hold the system tray icon

        # Initialize settings manager
        self.settings_manager = SettingsManager()
        self.settings = self.settings_manager.load_settings()

        # Set up automatic reconnection
        self.auto_reconnect = True

        # Load the icon for the system tray
        self.load_tray_icon()

        # UI Setup
        try:
            self.setup_ui()

            # Check if this is the first run
            if self.settings.get("first_run", True):
                self.show_first_run_wizard()
            else:
                self.show_auth_dialog()
        except Exception as e:
            logger.critical(f"Failed to initialize UI: {e}")
            messagebox.showerror(
                "Critical Error", f"Failed to initialize application: {e}"
            )
            raise

    def load_tray_icon(self):
        """Load the tray icon image"""
        try:
            # Try to load from file path
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "app_icon.ico"
            )
            if os.path.exists(icon_path):
                self.tray_icon = Image.open(icon_path)
            else:
                # Create a simple icon as fallback
                self.tray_icon = Image.new("RGB", (64, 64), color=(73, 109, 137))
        except Exception as e:
            logger.error(f"Error loading tray icon: {e}")
            # Create a simple icon as fallback
            self.tray_icon = Image.new("RGB", (64, 64), color=(73, 109, 137))

    def show_first_run_wizard(self):
        """Show the first run wizard to set up initial configuration"""
        wizard = tk.Toplevel(self.root)
        wizard.title("First Run Setup")
        wizard.geometry("500x450")
        wizard.transient(self.root)
        wizard.grab_set()  # Make window modal

        # Center the window
        window_width = 500
        window_height = 450
        screen_width = wizard.winfo_screenwidth()
        screen_height = wizard.winfo_screenheight()
        center_x = int(screen_width / 2 - window_width / 2)
        center_y = int(screen_height / 2 - window_height / 2)
        wizard.geometry(f"{window_width}x{window_height}+{center_x}+{center_y}")

        # Configure style
        frame = ttk.Frame(wizard, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        # Welcome message
        ttk.Label(
            frame, text="Welcome to Shomer Client", font=("Helvetica", 16, "bold")
        ).pack(pady=10)
        ttk.Label(
            frame, text="Please configure the following settings to get started."
        ).pack(pady=5)

        # Server URL
        ttk.Label(frame, text="Server Address:").pack(anchor="w", pady=(10, 0))
        server_url_var = tk.StringVar(value=DEFAULT_SERVER_URL)
        server_url_entry = ttk.Entry(frame, textvariable=server_url_var, width=40)
        server_url_entry.pack(fill=tk.X, pady=5)

        # Local WebSocket URI
        ttk.Label(frame, text="Local Core Application Port:").pack(
            anchor="w", pady=(10, 0)
        )
        local_ws_var = tk.StringVar(value=DEFAULT_LOCAL_WS_PORT)
        local_ws_entry = ttk.Entry(frame, textvariable=local_ws_var, width=40)
        local_ws_entry.pack(fill=tk.X, pady=5)

        # Admin password
        ttk.Label(frame, text="Admin Password (required for changing settings):").pack(
            anchor="w", pady=(10, 0)
        )
        password_var = tk.StringVar()
        password_entry = ttk.Entry(frame, textvariable=password_var, show="*", width=40)
        password_entry.pack(fill=tk.X, pady=5)

        # Confirm password
        ttk.Label(frame, text="Confirm Password:").pack(anchor="w", pady=(10, 0))
        confirm_var = tk.StringVar()
        confirm_entry = ttk.Entry(frame, textvariable=confirm_var, show="*", width=40)
        confirm_entry.pack(fill=tk.X, pady=5)

        # Error message label
        error_var = tk.StringVar()
        error_label = ttk.Label(frame, textvariable=error_var, foreground="red")
        error_label.pack(pady=10)

        def validate_and_save():
            # Validate input
            server_url = server_url_var.get().strip()
            local_ws = local_ws_var.get().strip()
            password = password_var.get()
            confirm = confirm_var.get()

            # Check all fields filled
            if not server_url or not local_ws or not password:
                error_var.set("All fields are required")
                return

            # Check password confirmation
            if password != confirm:
                error_var.set("Passwords do not match")
                return

            # Check password strength
            if len(password) < 8:
                error_var.set("Password must be at least 8 characters")
                return

            # Save settings
            try:
                self.settings_manager.save_setting("server_url", server_url)
                self.settings_manager.save_setting("local_ws_port", local_ws)
                self.settings_manager.save_setting("first_run", "False")

                # Set the admin password
                if not self.settings_manager.set_password(password):
                    error_var.set("Failed to set password")
                    return

                # Update in-memory settings
                self.settings = self.settings_manager.load_settings()

                # Close wizard and continue
                wizard.destroy()
                self.show_auth_dialog()
            except Exception as e:
                logger.error(f"Error saving initial settings: {e}")
                error_var.set(f"Error: {str(e)}")

        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, pady=15)

        ttk.Button(
            button_frame, text="Save & Continue", command=validate_and_save
        ).pack(side=tk.RIGHT)

        # Make sure wizard stays on top
        wizard.lift()
        wizard.focus_force()

        # Don't let user close this dialog
        def on_close():
            if messagebox.askyesno(
                "Exit Application", "Setup is not complete. Exit application?"
            ):
                self.root.destroy()

        wizard.protocol("WM_DELETE_WINDOW", on_close)

        # Wait for this window to be destroyed before continuing
        self.root.wait_window(wizard)

    def hide_to_tray(self):
        """Minimize the application to system tray"""
        self.root.withdraw()  # Hide the main window
        self.minimized_to_tray = True
        self.create_tray_icon()
        logger.info("Application minimized to system tray")

    def show_from_tray(self, icon=None, item=None):
        """Restore the application from system tray"""
        if self.icon:
            self.icon.stop()
            self.icon = None

        self.root.deiconify()  # Show the main window
        self.root.lift()  # Bring window to front
        self.minimized_to_tray = False
        logger.info("Application restored from system tray")

    def create_tray_icon(self):
        """Create and display the system tray icon with context menu"""
        menu = (
            item("Open", self.show_from_tray),
            item("Settings", self.show_settings_dialog),
            item("Exit", self.quit_app),
        )

        self.icon = pystray.Icon("Shomer_Client", self.tray_icon, "Shomer Client", menu)

        # Run the icon in a separate thread
        icon_thread = threading.Thread(target=self.icon.run)
        icon_thread.daemon = True
        icon_thread.start()

    def quit_app(self, icon=None, item=None):
        """Properly exit the application from tray menu"""
        logger.info("Exiting application from tray menu")
        if self.icon:
            self.icon.stop()
        self.shutdown()
        self.root.quit()

    def show_settings_dialog(self, icon=None, item=None):
        """Show the settings dialog with password protection"""
        # If minimized to tray, show the window first
        if self.minimized_to_tray:
            self.show_from_tray()

        # Ask for admin password
        password = simpledialog.askstring(
            "Authentication", "Enter admin password:", show="*", parent=self.root
        )

        if not password:
            return

        # Verify password
        if not self.settings_manager.check_password(password):
            messagebox.showerror("Error", "Invalid password")
            return

        # Create settings dialog
        settings_window = tk.Toplevel(self.root)
        settings_window.title("Application Settings")
        settings_window.geometry("500x300")
        settings_window.transient(self.root)
        settings_window.grab_set()  # Make window modal

        # Center the window
        window_width = 500
        window_height = 300
        screen_width = settings_window.winfo_screenwidth()
        screen_height = settings_window.winfo_screenheight()
        center_x = int(screen_width / 2 - window_width / 2)
        center_y = int(screen_height / 2 - window_height / 2)
        settings_window.geometry(
            f"{window_width}x{window_height}+{center_x}+{center_y}"
        )

        # Create settings form
        frame = ttk.Frame(settings_window, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        # Server URL
        ttk.Label(frame, text="Server Address:").pack(anchor="w", pady=(10, 0))
        server_url_var = tk.StringVar(
            value=self.settings.get("server_url", DEFAULT_SERVER_URL)
        )
        server_url_entry = ttk.Entry(frame, textvariable=server_url_var, width=40)
        server_url_entry.pack(fill=tk.X, pady=5)

        # Local WebSocket URI
        ttk.Label(frame, text="Local Core Application Port:").pack(
            anchor="w", pady=(10, 0)
        )
        local_ws_var = tk.StringVar(
            value=self.settings.get("local_ws_port", DEFAULT_LOCAL_WS_PORT)
        )
        local_ws_entry = ttk.Entry(frame, textvariable=local_ws_var, width=40)
        local_ws_entry.pack(fill=tk.X, pady=5)

        # Change password option
        ttk.Label(
            frame, text="Change Admin Password (leave blank to keep current):"
        ).pack(anchor="w", pady=(20, 0))

        # New password entry
        new_password_var = tk.StringVar()
        new_password_entry = ttk.Entry(
            frame, textvariable=new_password_var, show="*", width=40
        )
        new_password_entry.pack(fill=tk.X, pady=5)

        # Confirm new password entry
        ttk.Label(frame, text="Confirm New Password:").pack(anchor="w")
        confirm_var = tk.StringVar()
        confirm_entry = ttk.Entry(frame, textvariable=confirm_var, show="*", width=40)
        confirm_entry.pack(fill=tk.X, pady=5)

        # Error message label
        error_var = tk.StringVar()
        error_label = ttk.Label(frame, textvariable=error_var, foreground="red")
        error_label.pack(pady=10)

        def save_settings():
            # Validate and save new settings
            server_url = server_url_var.get().strip()
            local_ws = local_ws_var.get().strip()
            new_password = new_password_var.get()
            confirm_password = confirm_var.get()

            # Validate required fields
            if not server_url or not local_ws:
                error_var.set("Server URL and Local WebSocket URI are required")
                return

            # Validate password if changing
            if new_password:
                if new_password != confirm_password:
                    error_var.set("Passwords do not match")
                    return

                if len(new_password) < 8:
                    error_var.set("Password must be at least 8 characters")
                    return

                # Update password
                if not self.settings_manager.set_password(new_password):
                    error_var.set("Failed to update password")
                    return

            # Save settings
            try:
                # Update settings
                self.settings_manager.save_setting("server_url", server_url)
                self.settings_manager.save_setting("local_ws_port", local_ws)

                # Reload settings to memory
                self.settings = self.settings_manager.load_settings()

                # Close dialog
                settings_window.destroy()

                # Show confirmation
                messagebox.showinfo("Settings", "Settings updated successfully.")

                # Ask if user wants to reconnect with new settings
                if self.connected and messagebox.askyesno(
                    "Reconnect", "Would you like to reconnect with the new settings?"
                ):
                    self.disconnect()
                    self.connect()
            except Exception as e:
                logger.error(f"Error saving settings: {e}")
                error_var.set(f"Error: {str(e)}")

        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, pady=15)

        ttk.Button(button_frame, text="Cancel", command=settings_window.destroy).pack(
            side=tk.RIGHT, padx=5
        )

        ttk.Button(button_frame, text="Save", command=save_settings).pack(side=tk.RIGHT)

    def setup_ui(self):
        self.root.title("Shomer Client")
        self.root.geometry("800x600")
        self.root.minsize(640, 480)  # Set minimum window size

        # Create a style with more modern appearance
        self.style = ttk.Style()
        self.style.theme_use("alt")

        # Configure colors for better visibility
        self.style.configure("TFrame", background="#2d2d2d")
        self.style.configure("TLabel", background="#2d2d2d", foreground="white")
        self.style.configure(
            "TButton", background="#3d3d3d", foreground="white", padding=5
        )
        self.style.configure("Status.TLabel", font=("Helvetica", 10, "bold"))
        self.style.configure("Header.TLabel", font=("Helvetica", 12, "bold"), padding=5)
        self.style.configure("Success.TLabel", foreground="green")
        self.style.configure("Error.TLabel", foreground="red")
        self.style.configure("Warning.TLabel", foreground="orange")

        # Main container with a bit of padding
        main_frame = ttk.Frame(self.root, padding="10 10 10 10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Status bar with connection status
        self.status_frame = ttk.Frame(main_frame)
        self.status_frame.pack(fill=tk.X, pady=5)

        self.status_label = ttk.Label(
            self.status_frame, text="Disconnected", style="Status.TLabel"
        )
        self.status_label.pack(side=tk.LEFT)

        self.pc_label = ttk.Label(self.status_frame, text="")
        self.pc_label.pack(side=tk.RIGHT)

        # Control buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=5)

        self.connect_btn = ttk.Button(btn_frame, text="Connect", command=self.connect)
        self.connect_btn.pack(side=tk.LEFT, padx=2)
        self.createTooltip(self.connect_btn, "Connect to the server")

        self.disconnect_btn = ttk.Button(
            btn_frame, text="Disconnect", command=self.disconnect, state=tk.DISABLED
        )
        self.disconnect_btn.pack(side=tk.LEFT, padx=2)
        self.createTooltip(self.disconnect_btn, "Disconnect from the server")

        self.token_btn = ttk.Button(
            btn_frame, text="Change Token", command=self.change_token
        )
        self.token_btn.pack(side=tk.RIGHT, padx=2)
        self.createTooltip(self.token_btn, "Change authentication token")

        # Settings button
        self.settings_btn = ttk.Button(
            btn_frame, text="Settings", command=self.show_settings_dialog
        )
        self.settings_btn.pack(side=tk.RIGHT, padx=2)
        self.createTooltip(self.settings_btn, "Configure application settings")

        # Auto reconnect toggle
        self.reconnect_var = tk.BooleanVar(value=self.auto_reconnect)
        self.reconnect_check = ttk.Checkbutton(
            btn_frame,
            text="Auto Reconnect",
            command=self.toggle_auto_reconnect,
            variable=self.reconnect_var,
        )
        self.reconnect_check.pack(side=tk.RIGHT, padx=10)
        self.createTooltip(
            self.reconnect_check,
            "Automatically try to reconnect when connection is lost",
        )

        # Config view
        config_frame = ttk.Frame(main_frame)
        config_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # Create a label for the config header
        self.config_header = ttk.Label(
            config_frame, text="Current Configuration", style="Header.TLabel"
        )
        self.config_header.pack(fill=tk.X, pady=(5, 0))

        # Configuration view with better styling
        self.config_view = scrolledtext.ScrolledText(
            config_frame,
            height=10,
            bg="#1d1d1d",
            fg="white",
            insertbackground="white",
            font=("Consolas", 10),
            wrap=tk.WORD,
            padx=5,
            pady=5,
        )
        self.config_view.pack(fill=tk.BOTH, expand=True, pady=5)

        # Debug panel (hidden by default in production)
        self.debug_frame = ttk.Frame(main_frame)
        self.debug_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.debug_toggle = ttk.Button(
            self.debug_frame, text="▼ Debug", command=self.toggle_debug
        )
        self.debug_toggle.pack(fill=tk.X)

        self.debug_area = scrolledtext.ScrolledText(
            self.debug_frame,
            height=8,
            bg="#1d1d1d",
            fg="white",
            insertbackground="white",
            font=("Consolas", 9),
            wrap=tk.WORD,
            padx=5,
            pady=5,
        )

        # Hide debug area by default for production
        if not self.debug_visible:
            self.debug_area.pack_forget()
        else:
            self.debug_area.pack(fill=tk.BOTH, expand=True)

        # Notification area with timestamps
        self.notif_frame = ttk.Frame(main_frame)
        self.notif_frame.pack(fill=tk.X, pady=5)

        notif_label = ttk.Label(
            self.notif_frame, text="Notifications", style="Header.TLabel"
        )
        notif_label.pack(fill=tk.X)

        self.notif_area = tk.Listbox(
            self.notif_frame,
            height=6,
            bg="#3d3d3d",
            fg="white",
            selectbackground="#4d4d4d",
            font=("Consolas", 9),
            borderwidth=0,
        )
        self.notif_area.pack(fill=tk.X, pady=5)

        # Add a clear button for notifications
        clear_btn = ttk.Button(
            self.notif_frame,
            text="Clear Notifications",
            command=lambda: self.notif_area.delete(0, tk.END),
        )
        clear_btn.pack(side=tk.RIGHT, padx=2, pady=2)

        # Set up the minimize to tray behavior
        self.root.protocol("WM_DELETE_WINDOW", self.handle_close)

        self.update_status()

    def handle_close(self):
        """Handle window close event by minimizing to tray instead of closing"""
        self.hide_to_tray()

    def createTooltip(self, widget, text):
        """Create a tooltip for a widget"""

        def enter(event):
            x, y, _, _ = widget.bbox("insert")
            x += widget.winfo_rootx() + 25
            y += widget.winfo_rooty() + 25

            # Create a toplevel window
            self.tooltip = tk.Toplevel(widget)
            self.tooltip.wm_overrideredirect(True)
            self.tooltip.wm_geometry(f"+{x}+{y}")

            label = ttk.Label(
                self.tooltip,
                text=text,
                justify=tk.LEFT,
                background="#ffffe0",
                relief=tk.SOLID,
                borderwidth=1,
                font=("Segoe UI", 9, "normal"),
            )
            label.pack(ipadx=5, ipady=2)

        def leave(event):
            if hasattr(self, "tooltip"):
                self.tooltip.destroy()

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    def toggle_auto_reconnect(self):
        self.auto_reconnect = self.reconnect_var.get()
        self.log_debug(f"Auto reconnect set to: {self.auto_reconnect}")

    def toggle_debug(self):
        self.debug_visible = not self.debug_visible
        if self.debug_visible:
            self.debug_area.pack(fill=tk.BOTH, expand=True)
            self.debug_toggle.config(text="▼ Debug")
        else:
            self.debug_area.pack_forget()
            self.debug_toggle.config(text="▲ Debug")

    def log_debug(self, message):
        """Add a message to the debug area with timestamp"""
        try:
            timestamp = time.strftime("%H:%M:%S")
            self.debug_area.insert(tk.END, f"[{timestamp}] {message}\n")
            self.debug_area.see(tk.END)
        except Exception as e:
            logger.error(f"Failed to log debug message: {e}")

    def show_notification(self, message):
        """Add a notification to the notification area with timestamp"""
        try:
            timestamp = time.strftime("%H:%M:%S")
            self.notif_area.insert(0, f"[{timestamp}] {message}")
            if self.notif_area.size() > 50:  # Limit to 50 entries
                self.notif_area.delete(50, tk.END)
        except Exception as e:
            logger.error(f"Failed to show notification: {e}")

    def update_status(self):
        """Update the UI based on current connection status"""
        try:
            status_text = "Connected" if self.connected else "Disconnected"
            status_color = "green" if self.connected else "red"
            self.status_label.config(text=status_text, foreground=status_color)

            # Update PC info display
            if self.pc_info["id"]:
                pc_text = f"{self.pc_info['name']} ({self.pc_info['id']})"
                self.pc_label.config(text=pc_text)
            else:
                self.pc_label.config(text="")

            # Update button states
        if self.connected:
            self.connect_btn.config(state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
                # Disable token change while connected
                self.token_btn.config(state=tk.DISABLED)
        else:
                self.connect_btn.config(
                    state=tk.NORMAL if self.pc_info["id"] else tk.DISABLED
                )
            self.disconnect_btn.config(state=tk.DISABLED)
                self.token_btn.config(state=tk.NORMAL)
        except Exception as e:
            logger.error(f"Error updating status: {e}")

    def show_auth_dialog(self):
        # Don't show dialog if app is exiting
        if self.exit_flag:
            return

        token_prompt = "Enter JWT Token:"

        token = simpledialog.askstring("Authentication", token_prompt, parent=self.root)

        if token is None:  # User canceled
            if not self.pc_info["id"]:  # If no existing token, exit the application
                if messagebox.askyesno(
                    "Exit Application", "No token provided. Exit application?"
                ):
                    self.shutdown()
                    self.root.quit()
            return

        self.handle_new_token(token)

    def handle_new_token(self, token):
        if not token:
            self.log_debug("No token provided")
            return

        try:
            # Decode the token to extract PC information
            try:
                # Use JWT_SECRET from settings if available
                jwt_secret = self.settings.get("jwt_secret", "your-secret-key")
                payload = jwt.decode(token, jwt_secret, algorithms=["HS256"])
            except jwt.ExpiredSignatureError:
                message = "Token has expired. Please provide a valid token."
                logger.error(message)
                messagebox.showerror("Token Error", message)
                return
            except jwt.InvalidTokenError as e:
                message = f"Invalid token: {str(e)}"
                logger.error(message)
                messagebox.showerror("Token Error", message)
                return

            # Extract PC information from token payload
            self.pc_info = {
                "name": payload.get("name", payload.get("pc_name", "Unknown")),
                "role": payload.get("role", "controller"),
                "id": payload.get("pc_id", ""),
            }

            if not self.pc_info["id"]:
                messagebox.showerror("Token Error", "Token does not contain a PC ID")
                return

            logger.info(
                f"Token decoded: PC ID={self.pc_info['id']}, Name={self.pc_info['name']}"
            )

            # Clear the existing socket.io client if connected
            if self.connected:
                self.disconnect()

            # Store token for use in connection
            self.current_token = token

            # Initialize a new Socket.IO client
            self.initialize_socketio()

            # Connect with the new token information
            self.connect()
            self.update_status()
            self.log_debug(f"New token accepted for {self.pc_info['name']}")
            self.show_notification(
                f"Authenticated as {self.pc_info['name']} ({self.pc_info['id']})"
            )
        except Exception as e:
            logger.error(f"Error handling token: {e}")
            messagebox.showerror("Token Error", f"Error processing token: {str(e)}")
            self.show_notification(f"Error with token: {str(e)}")
            import traceback

            logger.error(traceback.format_exc())

    def initialize_socketio(self):
        """Initialize a new Socket.IO client with event handlers and error handling"""
        try:
            # Clean up previous socket.io instance if it exists
            if self.sio:
                try:
                    if self.connected:
                        self.sio.disconnect()
                except:
                    pass
                self.sio = None

            # Create a new socket.io client with appropriate settings
            self.sio = socketio.Client(
                logger=True,
                engineio_logger=True,
                reconnection=False,
                request_timeout=10,
                http_session=None,  # Create a fresh session
            )

            # Set up event handlers
            @self.sio.on("connect")
            def on_connect():
                logger.info("Connected to server")
                self.connected = True
                self.reconnect_attempts = (
                    0  # Reset reconnect counter on successful connection
                )
                self.root.after(0, self.update_status)
                self.root.after(
                    0, lambda: self.show_notification("Connected to server")
                )

                # Register with server after connection
                logger.info(f"Registering as {self.pc_info['id']}")
                if self.pc_info["id"] and self.current_token:
                    try:
                        self.sio.emit(
                            "register",
                            {
                                "pc_id": self.pc_info["id"],
                                "auth_token": self.current_token,
                            },
                        )
                        logger.info(
                            f"Registration request sent for {self.pc_info['id']}"
                        )
                    except Exception as emit_err:
                        logger.error(f"Failed to send registration: {emit_err}")
                else:
                    logger.error("Cannot register: Missing PC ID or token")
                    self.root.after(
                        0,
                        lambda: self.show_notification("Error: Missing PC ID or token"),
                    )

            @self.sio.on("disconnect")
            def on_disconnect():
                logger.info("Disconnected from server")
                self.connected = False
                self.root.after(0, self.update_status)
                self.root.after(
                    0, lambda: self.show_notification("Disconnected from server")
                )

                # Try to reconnect if auto reconnect is enabled and not exiting app
                if (
                    not self.exit_flag
                    and self.auto_reconnect
                    and self.reconnect_attempts < self.max_reconnect_attempts
                ):
                    # Exponential backoff
                    delay = min(30, 2**self.reconnect_attempts)
                    self.reconnect_attempts += 1
                    self.root.after(
                        0,
                        lambda: self.show_notification(
                            f"Reconnecting in {delay} seconds (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})"
                        ),
                    )
                    logger.info(
                        f"Scheduling reconnection in {delay} seconds (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})"
                    )

                    # Cancel any existing reconnect timer
                    if self.reconnect_timer:
                        self.root.after_cancel(self.reconnect_timer)

                    # Schedule reconnection
                    self.reconnect_timer = self.root.after(delay * 1000, self.connect)

            @self.sio.on("connect_error")
            def on_connect_error(data):
                logger.error(f"Connection error: {data}")
                self.root.after(
                    0, lambda: self.show_notification(f"Connection error: {data}")
                )

                # Force disconnect state to ensure UI consistency
                self.connected = False
                self.root.after(0, self.update_status)

            @self.sio.on("config")
            def on_config(data):
                logger.info(f"Received config message from server")
                try:
                    logger.debug(f"Config data type: {type(data).__name__}")
                    logger.debug(
                        f"Config data: {json.dumps(data, indent=2) if isinstance(data, dict) else str(data)[:200]}"
                    )

                    # Extract configuration content
                    sender = (
                        data.get("from", "unknown")
                        if isinstance(data, dict)
                        else "unknown"
                    )
                    content = (
                        data.get("content", {}) if isinstance(data, dict) else data
                    )

                    self.root.after(
                        0,
                        lambda: self.show_notification(
                            f"Received configuration from {sender}"
                        ),
                    )
                    self.root.after(0, lambda: self.process_config(content))
                except Exception as e:
                    logger.error(f"Error handling config message: {e}")
                    import traceback

                    logger.error(traceback.format_exc())
                    self.root.after(
                        0,
                        lambda: self.show_notification(
                            f"Error processing config message: {str(e)}"
                        ),
                    )

            @self.sio.on("message")
            def on_message(data):
                try:
                    logger.info(
                        f"Received message: {json.dumps(data, indent=2) if isinstance(data, dict) else str(data)}"
                    )
                    message_text = (
                        data.get("message", "") if isinstance(data, dict) else str(data)
                    )
                    status = data.get("status", "") if isinstance(data, dict) else ""

                    if status == "success" and "registration" in message_text.lower():
                        self.root.after(
                            0,
                            lambda: self.show_notification(
                                f"Successfully registered with server"
                            ),
                        )
        else:
                        self.root.after(
                            0,
                            lambda: self.show_notification(f"Message: {message_text}"),
                        )
                except Exception as e:
                    logger.error(f"Error handling message event: {e}")
                    self.root.after(
                        0, lambda: self.log_debug(f"Error handling message: {str(e)}")
                    )

            @self.sio.on("error")
            def on_error(data):
                try:
                    logger.error(
                        f"Received error: {json.dumps(data, indent=2) if isinstance(data, dict) else str(data)}"
                    )
                    error_msg = (
                        data.get("message", "Unknown error")
                        if isinstance(data, dict)
                        else str(data)
                    )
                    self.root.after(
                        0, lambda: self.show_notification(f"Error: {error_msg}")
                    )
                    self.log_debug(f"Server error: {error_msg}")
                except Exception as e:
                    logger.error(f"Error handling error event: {e}")

            @self.sio.on("warning")
            def on_warning(data):
                try:
                    logger.warning(
                        f"Received warning: {json.dumps(data, indent=2) if isinstance(data, dict) else str(data)}"
                    )
                    warning_msg = (
                        data.get("message", "Unknown warning")
                        if isinstance(data, dict)
                        else str(data)
                    )
                    self.root.after(
                        0, lambda: self.show_notification(f"Warning: {warning_msg}")
                    )
                    self.log_debug(f"Server warning: {warning_msg}")
                except Exception as e:
                    logger.error(f"Error handling warning event: {e}")

            @self.sio.on("clients_update")
            def on_clients_update(data):
                try:
                    logger.info(f"Clients update: {data}")
                    client_count = len(data) if isinstance(data, list) else "unknown"
                    self.root.after(
                        0, lambda: self.log_debug(f"Connected clients: {client_count}")
                    )

                    # Check if we're in the list of connected clients
                    if isinstance(data, list) and self.pc_info["id"] in data:
                        self.root.after(
                            0,
                            lambda: self.show_notification(
                                f"Confirmed connection with server"
                            ),
                        )
                    else:
                        self.root.after(
                            0,
                            lambda: self.show_notification(
                                f"Not registered with server"
                            ),
                        )
                except Exception as e:
                    logger.error(f"Error handling clients_update event: {e}")

            logger.info("Socket.IO client initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing Socket.IO client: {e}")
            self.root.after(
                0,
                lambda: self.show_notification(
                    f"Error initializing connection: {str(e)}"
                ),
            )
            import traceback

            logger.error(traceback.format_exc())

    def change_token(self):
        self.show_auth_dialog()

    def process_config(self, config):
        """Process incoming configuration with robust error handling"""
        try:
            if not config:
                logger.warning("Received empty configuration")
                self.root.after(
                    0, lambda: self.show_notification("Received empty configuration")
                )
                return

            logger.info(f"Processing configuration of type: {type(config).__name__}")

            # Handle string configs by attempting to parse as JSON
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                    logger.info("Successfully parsed string config as JSON")
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse config string as JSON: {e}")
                    self.show_notification(f"Error: Invalid JSON in configuration")
                    self.log_debug(f"JSON parse error: {str(e)}")
                    return

            # Validate configuration has minimum required structure
            if not isinstance(config, dict):
                logger.error(f"Invalid configuration type: {type(config).__name__}")
                self.show_notification("Error: Invalid configuration format")
                return

            self.current_config = config
            logger.info(f"Configuration size: {len(json.dumps(config))} bytes")

            # Add to config history (keep last 5)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.config_history.append((timestamp, config))
            if len(self.config_history) > 5:
                self.config_history.pop(0)

            # Display the config in the UI
            self.display_config(config)

            # Send to local websocket if available - in a daemon thread to avoid blocking
            ws_thread = threading.Thread(target=self.send_to_local_ws, args=(config,))
            ws_thread.daemon = True
            ws_thread.start()

            # Save to file for debug purposes
            config_dir = os.path.join(
                os.path.expanduser("~"), ".Shomer_Client", "configs"
            )
            try:
                os.makedirs(config_dir, exist_ok=True)
                # Save with timestamp to keep history
                filename = os.path.join(config_dir, "last_config.json")
                with open(filename, "w") as f:
                    json.dump(config, f, indent=2)
                    logger.info(f"Saved config to {filename}")
            except Exception as e:
                logger.error(f"Failed to save config to file: {e}")
                self.log_debug(f"Warning: Could not save config to file: {str(e)}")

            self.log_debug(f"Configuration successfully processed")
            self.show_notification("Configuration processed and applied")
        except Exception as e:
            logger.error(f"Error processing config: {str(e)}")
            self.root.after(
                0, lambda: self.show_notification(f"Error processing config: {str(e)}")
            )
            import traceback

            tb = traceback.format_exc()
            logger.error(f"Traceback: {tb}")
            self.log_debug(f"Error: {str(e)}\n{tb}")

    def display_config(self, config):
        """Display the configuration in the config view with error handling"""
        try:
            # Clear the current content
            self.config_view.delete("1.0", tk.END)
            # Ensure we can modify the text
            self.config_view.config(state=tk.NORMAL)

            # Format the config for display
            if isinstance(config, dict):
                # Create a human-readable summary
                summary = self.create_config_summary(config)
                if summary:
                    self.config_view.insert(
                        "1.0",
                        f"== CONFIGURATION SUMMARY ==\n{summary}\n\n== FULL CONFIGURATION JSON ==\n",
                    )

                # Pretty print the JSON with indentation
                try:
                    formatted_config = json.dumps(config, indent=4)
                    self.config_view.insert(tk.END, formatted_config)
                except Exception as json_err:
                    logger.error(f"Error formatting JSON: {json_err}")
                    self.config_view.insert(
                        tk.END, f"Error formatting JSON: {json_err}\n{str(config)}"
                    )
            else:
                self.config_view.insert(
                    tk.END,
                    f"Invalid configuration format: {type(config)}\n{str(config)}",
                )

            # Make it read-only to prevent accidental edits
            self.config_view.config(state=tk.DISABLED)
        except Exception as e:
            logger.error(f"Error displaying config: {str(e)}")
            try:
                self.config_view.config(state=tk.NORMAL)
                self.config_view.delete("1.0", tk.END)
                self.config_view.insert(
                    tk.END, f"Error displaying configuration: {str(e)}"
                )
                self.config_view.config(state=tk.DISABLED)
            except:
                pass

    def create_config_summary(self, config):
        """Create a human-readable summary of the configuration with error handling"""
        try:
            summary = StringIO()

            # Extract PC info
            if "pcs" in config and isinstance(config["pcs"], dict):
                summary.write(f"PCs configured: {len(config['pcs'])}\n")
                for pc_id, pc_info in config["pcs"].items():
                    if not isinstance(pc_info, dict):
                        continue
                    summary.write(f"- PC {pc_info.get('name', pc_id)}\n")

                    # Extract screen info
                    screens = pc_info.get("screens", {})
                    if screens and isinstance(screens, dict):
                        summary.write(f"  Screens: {len(screens)}\n")
                        for screen_id, screen_info in screens.items():
                            if not isinstance(screen_info, dict):
                                continue
                            summary.write(
                                f"  - Screen {screen_info.get('name', screen_id)}: "
                            )
                            layout = screen_info.get("layout", {})
                            if isinstance(layout, dict):
                                summary.write(f"{layout.get('rows', '?')}x")
                                summary.write(f"{layout.get('columns', '?')}\n")
                            else:
                                summary.write("Invalid layout\n")

            # Check if this is a direct configuration format
            if "screens" in config:
                summary.write(f"Direct Configuration\n")
                summary.write(
                    f"- Size: {config.get('width', '?')}x{config.get('height', '?')}\n"
                )

                screens = config.get("screens", [])
                if isinstance(screens, list):
                    summary.write(f"- Screens: {len(screens)}\n")

                    for i, screen in enumerate(screens):
                        if not isinstance(screen, dict):
                            continue
                        screen_id = screen.get("id", f"screen_{i}")
                        switch_interval = screen.get("switchInterval", "?")
                        summary.write(
                            f"  - Screen {screen_id} (switch interval: {switch_interval}s)\n"
                        )

                        # Source groups
                        source_groups = screen.get("source_groups", [])
                        if source_groups and isinstance(source_groups, list):
                            summary.write(f"    Source groups: {len(source_groups)}\n")
                            for j, group in enumerate(source_groups):
                                if group and isinstance(group, list):
                                    summary.write(
                                        f"    - Group {j+1}: {len(group)} sources\n"
                                    )
                                    for k, source in enumerate(group):
                                        if (
                                            source
                                            and isinstance(source, dict)
                                            and source.get("id")
                                        ):
                                            summary.write(
                                                f"      - Source {k+1}: {source.get('id')}\n"
                                            )
                                            summary.write(
                                                f"        URL: {source.get('url', 'N/A')}\n"
                                            )

            # Extract screen to camera mappings
            if (
                "mappings" in config
                and isinstance(config["mappings"], dict)
                and "screen_to_cameras" in config["mappings"]
            ):
                mappings = config["mappings"]["screen_to_cameras"]
                if isinstance(mappings, dict):
                    for pc_id, pc_mappings in mappings.items():
                        if not isinstance(pc_mappings, dict) or not pc_mappings:
                            continue
                        summary.write(f"\nCamera mappings for PC {pc_id}:\n")
                        for screen_id, views in pc_mappings.items():
                            if not isinstance(views, dict):
                                continue
                            for view_id, slots in views.items():
                                if not isinstance(slots, dict):
                                    continue
                                for slot_key, camera_info in slots.items():
                                    if (
                                        isinstance(camera_info, dict)
                                        and "camera_id" in camera_info
                                    ):
                                        summary.write(
                                            f"  - {slot_key}: Camera {camera_info.get('camera_id')}"
                                        )
                                        summary.write(
                                            f" ({camera_info.get('camera_name', 'Unnamed')})\n"
                                        )

            return summary.getvalue()
        except Exception as e:
            logger.error(f"Error creating config summary: {str(e)}")
            return f"Error creating summary: {str(e)}"

    def send_to_local_ws(self, config):
        async def async_send():
            try:
                # Get the local websocket URI from settings
                local_ws_port = self.settings.get(
                    "local_ws_port", DEFAULT_LOCAL_WS_PORT
                )
                local_ws_uri = f"ws://localhost:{local_ws_uri}"

                logger.info(f"Connecting to local WS at {local_ws_uri}")
                try:
                    # Add connection and operation timeouts
                    async with websockets.connect(
                        local_ws_uri,
                        ping_timeout=10,
                        close_timeout=5,
                        max_size=10 * 1024 * 1024,  # 10MB max message size
                    ) as ws:
                        logger.info("Connected to local WebSocket")
                        await ws.send(json.dumps(config))
                        logger.info("Config sent to local WS")
                        self.root.after(
                            0,
                            lambda: self.show_notification(
                                "Configuration sent to local service"
                            ),
                        )
                except websockets.exceptions.WebSocketException as ws_err:
                    logger.warning(f"Local WebSocket service not available: {ws_err}")
                    self.root.after(
                        0,
                        lambda: self.log_debug(
                            f"Local service not available. Config applied but not forwarded."
                        ),
                    )
            except asyncio.CancelledError:
                logger.info("WebSocket operation cancelled")
            except Exception as e:
                logger.error(f"WS Error: {str(e)}")
                self.root.after(
                    0, lambda: self.show_notification(f"Local WS error: {str(e)}")
                )

        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # Set a timeout for the operation
            try:
                loop.run_until_complete(asyncio.wait_for(async_send(), timeout=15.0))
            except asyncio.TimeoutError:
                logger.warning("WebSocket communication timed out")
                self.root.after(
                    0,
                    lambda: self.show_notification(
                        "Local service communication timed out"
                    ),
                )
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"Error in send_to_local_ws: {str(e)}")

    def connect(self):
        if self.reconnect_timer:
            self.root.after_cancel(self.reconnect_timer)
            self.reconnect_timer = None

        if not self.pc_info["id"] or not self.current_token:
            messagebox.showerror(
                "Error", "No PC ID or token available. Please set a valid token first."
            )
            return

        # Don't try to connect if already connecting or connected
        if self.connected or (
            self.connection_thread and self.connection_thread.is_alive()
        ):
            logger.info("Already connected or connection in progress")
            self.show_notification("Connection already in progress...")
            return

        try:
            # Create a new Socket.IO client if needed
            if not self.sio:
                self.initialize_socketio()

            # Get server URL from settings
            server_url = self.settings.get("server_url", DEFAULT_SERVER_URL)

            logger.info(f"Connecting to server at {server_url} as {self.pc_info['id']}")
            self.show_notification(f"Connecting to server...")

            # Update UI to show connecting state
            self.connect_btn.config(state=tk.DISABLED)
            self.status_label.config(text="Connecting...", foreground="orange")

            # Connect in a separate thread to avoid blocking UI
            self.connection_thread = threading.Thread(
                target=self._connect_thread, args=(server_url,)
            )
            # Make thread daemon so it doesn't block app exit
            self.connection_thread.daemon = True
            self.connection_thread.start()
        except Exception as e:
            logger.error(f"Connection preparation error: {str(e)}")
            messagebox.showerror("Connection Error", str(e))
            self.update_status()

    def _connect_thread(self, server_url):
        """Connect to the server in a separate thread"""
        try:
            if self.exit_flag:
                return

            # Add connection timeout
            self.sio.connect(server_url, wait_timeout=10)
            logger.info("Connected successfully")
        except socketio.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {str(e)}")
            self.root.after(
                0, lambda: self.show_notification(f"Connection error: {str(e)}")
            )
            # Force disconnect state to ensure UI consistency
            self.connected = False
            self.root.after(0, self.update_status)
            # The disconnect handler will handle reconnection attempts if enabled
        except Exception as e:
            logger.error(f"Unexpected connection error: {str(e)}")
            self.root.after(
                0, lambda: self.show_notification(f"Connection error: {str(e)}")
            )
            self.connected = False
            self.root.after(0, self.update_status)

    def disconnect(self):
        if self.reconnect_timer:
            self.root.after_cancel(self.reconnect_timer)
            self.reconnect_timer = None

        try:
            if self.sio and self.connected:
                self.sio.disconnect()
            logger.info("Disconnected from server")
        except Exception as e:
            logger.error(f"Error disconnecting: {str(e)}")
        finally:
            self.connected = False
            self.update_status()

    def shutdown(self):
        """Properly shutdown all resources"""
        logger.info("Shutting down application")
        self.exit_flag = True

        # Cancel any pending reconnects
        if self.reconnect_timer:
            self.root.after_cancel(self.reconnect_timer)
            self.reconnect_timer = None

        # Disconnect from server
        try:
            if self.sio and self.connected:
                self.sio.disconnect()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

        # Wait for connection thread if it's running
        if self.connection_thread and self.connection_thread.is_alive():
            self.connection_thread.join(timeout=1.0)

        # Stop tray icon if active
        if self.icon:
            try:
                self.icon.stop()
                self.icon = None
            except Exception as e:
                logger.error(f"Error stopping tray icon: {e}")

        logger.info("Application shutdown complete")


def main():
    # Create the Tkinter root
    root = tk.Tk()

    # Set application icon for Windows
    try:
        if sys.platform == "win32":
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "app_icon.ico"
            )
            if os.path.exists(icon_path):
                root.iconbitmap(default=icon_path)
    except Exception as e:
        logger.warning(f"Could not set application icon: {e}")

    # Enable DPI awareness on Windows
    try:
        if sys.platform == "win32":
            from ctypes import windll

            windll.shcore.SetProcessDpiAwareness(1)
    except Exception as e:
        logger.warning(f"Could not set DPI awareness: {e}")

    # Handle uncaught exceptions
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            # Don't log keyboard interrupt
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        # Log the exception
        logger.critical(
            "Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback)
        )

        # Show error message to user
        error_msg = f"An unexpected error occurred:\n{exc_type.__name__}: {exc_value}"
        try:
            if root and root.winfo_exists():
                messagebox.showerror("Application Error", error_msg)
        except:
            pass

    # Set the exception handler
    sys.excepthook = handle_exception

    try:
        app = ModernClient(root)

        # Center the window
        window_width = 800
        window_height = 600
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        center_x = int(screen_width / 2 - window_width / 2)
        center_y = int(screen_height / 2 - window_height / 2)
        root.geometry(f"{window_width}x{window_height}+{center_x}+{center_y}")

        # Set application title
        root.title("Shomer Client")

        # Handle window close properly
        def on_closing():
            try:
                # Instead of shutting down, minimize to tray
                app.hide_to_tray()
            except Exception as e:
                logger.error(f"Error hiding to tray: {e}")
                try:
                    app.shutdown()
                    root.destroy()
                except Exception as e2:
                    logger.error(f"Error during application close: {e2}")
                    root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_closing)

        # Start the Tkinter event loop
    root.mainloop()
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}")
        traceback.print_exc()
        try:
            messagebox.showerror("Critical Error", f"Application failed to start: {e}")
        except:
            pass


if __name__ == "__main__":
    main()
