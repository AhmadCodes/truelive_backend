import json
from typing import Dict, Any
import uuid

def read_json_file(file_path: str) -> Dict[str, Any]:
    """
    Reads a JSON file and returns its contents as a dictionary.
    """
    try:
        with open(file_path, 'r') as file:
            return json.load(file)
    except FileNotFoundError:
        print(f"Error: File {file_path} not found")
        return {}
    except json.JSONDecodeError:
        print(f"Error: File {file_path} contains invalid JSON")
        return {}

def build_rtsp_url(server: Dict[str, Any], camera: Dict[str, Any]) -> str:
    """
    Builds RTSP URL from server and camera information.
    """
    extra_value = server['extraValue'].replace('{#}', str(camera['input1']))
    return f"rtsp://{server['username']}:{server['password']}@{server['host']}:{server['port']}/{extra_value}"

def generate_camera_key(device_id: int) -> str:
    """
    Generates a camera key using the device ID.
    """
    return f"{device_id}"

def generate_site_key(server_id: int) -> str:
    """
    Generates a site key using the server ID.
    """
    return f"{server_id}"

def merge_configurations(camera_config: Dict[str, Any] = 'GetServerList.json', 
                       server_list: Dict[str, Any] = 'AdvancedDevices_ByType_1.json', 
                       device_list: Dict[str, Any] = 'camera_config.json') -> Dict[str, Any]:
    """
    Merges new server and camera information into existing camera configuration.
    """
    # Initialize sites if not present
    if 'sites' not in camera_config:
        camera_config['sites'] = {}
    
    # Create a mapping of server IDs to server data
    server_map = {server['serverID']: server for server in server_list['data']}
    
    # Process each device/camera
    for device in device_list['data']:
        server_id = device['serverID']
        
        # Skip if server not found in server list
        if server_id not in server_map:
            continue
            
        server = server_map[server_id]
        site_key = generate_site_key(server_id)
        camera_key = generate_camera_key(device['deviceID'])
        
        # Add new site if it doesn't exist
        if site_key not in camera_config['sites']:
            camera_config['sites'][site_key] = {
                'name': server['title'],
                'nvr_username': server['username'],
                'nvr_password': server['password'],
                'cameras': {}
            }
        
        # Add new camera if it doesn't exist
        if camera_key not in camera_config['sites'][site_key]['cameras']:
            camera_config['sites'][site_key]['cameras'][camera_key] = {
                'name': device['title'],
                'rtsp_url': build_rtsp_url(server, device)
            }
    
    return camera_config

def update_camera_config(server_list_path: str, 
                        device_list_path: str, 
                        camera_config_path: str, 
                        output_path: str = None) -> None:
    """
    Main function to update camera configuration.
    """
    # Read input files
    server_list = read_json_file(server_list_path)
    device_list = read_json_file(device_list_path)
    camera_config = read_json_file(camera_config_path)
    
    if not all([server_list, device_list, camera_config]):
        print("Error: Failed to read one or more input files")
        return
    
    # Merge configurations
    updated_config = merge_configurations(camera_config, server_list, device_list)
    
    # Write output
    output_path = output_path or camera_config_path
    try:
        with open(output_path, 'w') as file:
            json.dump(updated_config, file, indent=4)
        print(f"Successfully updated configuration at {output_path}")
    except Exception as e:
        print(f"Error writing output file: {str(e)}")

# Example usage
if __name__ == "__main__":
    update_camera_config(
        server_list_path="GetServerList.json",
        device_list_path="AdvancedDevices_ByType_1.json",
        camera_config_path="camera_config.json"
    )