import asyncio
import websockets
import json
import os

def is_running_in_docker():
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            return "docker" in f.read()
    except:
        return False

ws_port = os.getenv('WS_PORT', 9022)
    
async def send_config(config):
    # if program is running in docker container, use host.docker.internal to access host machine
    if is_running_in_docker():
        uri = f'ws://host.docker.internal:{ws_port}'
    else:
        uri = f'ws://localhost:{ws_port}'
    async with websockets.connect(uri) as websocket:
        config_json = json.dumps(config)
        await websocket.send(config_json)

def send_config_sync(config):
    try:
        # Check if an event loop is already running
        loop = asyncio.get_running_loop()
    except RuntimeError:  # No event loop is running
        loop = None

    if loop:
        # If an event loop is running, schedule the coroutine
        loop.create_task(send_config(config))
    else:
        # If no event loop is running, use asyncio.run()
        asyncio.run(send_config(config))

if __name__ == "__main__":
    import os
    cofigpath = os.path.join(os.path.dirname(__file__), "..", 'st_config.json')
    if not os.path.exists(cofigpath):
        print(f"Config file not found at {cofigpath}")
        exit(1)
    with open(cofigpath, 'r') as file:
        config = json.load(file)
    send_config_sync(config)