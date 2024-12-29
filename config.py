# config.py
import os
from dataclasses import dataclass

@dataclass
class Config:
    CAMERA_CONFIG_FILE = os.getenv('CAMERA_CONFIG_FILE', 'camera_config.json')
    SITE_CONFIG_FILE = os.getenv('SITE_CONFIG_FILE', 'site_config.json')
    STREAM_APP_WS_URL = os.getenv('STREAM_APP_WS_URL', 'ws://localhost:8765')