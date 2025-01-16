# config.py
import os
from dataclasses import dataclass

@dataclass
class Config:
    DB_PATH = os.getenv('DB_PATH', 'config.db')
    STREAM_APP_WS_URL = os.getenv('STREAM_APP_WS_URL', 'ws://localhost:8765')