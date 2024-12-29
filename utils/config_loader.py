import json
from config import Config

def load_camera_config():
    with open(Config.CAMERA_CONFIG_FILE, 'r') as f:
        return json.load(f)

def load_site_config():
    with open(Config.SITE_CONFIG_FILE, 'r') as f:
        return json.load(f)

def save_camera_config(config):
    with open(Config.CAMERA_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def save_site_config(config):
    with open(Config.SITE_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)