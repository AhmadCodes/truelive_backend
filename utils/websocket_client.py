import asyncio
import websockets
import json

async def send_config(config):
    uri = 'ws://host.docker.internal:9022'
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