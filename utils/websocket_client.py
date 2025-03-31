import asyncio
import socketio
import json
import os
import jwt
import time
import logging
import uuid
import traceback

# Set up logging
logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('websocket_client')

def is_running_in_docker():
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup") as f:
            return "docker" in f.read()
    except:
        return False

# Get Socket.IO server URL from environment variable
SERVER_URL = os.getenv('AWS_WS_URL', 'http://18.204.201.19:8080/')
logger.info(f"Using server URL: {SERVER_URL}")

# JWT secret key for validation
JWT_SECRET = os.getenv('JWT_SECRET', 'your-secret-key')

# Message response tracking
message_responses = {}

async def send_config(config, pc_id, auth_token):
    """
    Send configuration to a specific PC through Socket.IO server
    
    Args:
        config: The configuration to send
        pc_id: The ID of the PC to send the config to
        auth_token: JWT token for authentication
    """
    sio = None  # Initialize outside try block for proper cleanup
    
    try:
        # Clear any previous responses
        message_responses.clear()
        
        # Verify the authentication token and get PC ID if different
        try:
            payload = jwt.decode(auth_token, JWT_SECRET, algorithms=['HS256'])
            token_pc_id = payload.get('pc_id')
            
            # Verify token matches target PC
            if token_pc_id != pc_id:
                logger.warning(f"Token PC ID ({token_pc_id}) doesn't match target PC ID ({pc_id})")
                logger.info(f"Using token PC ID ({token_pc_id}) as target")
                pc_id = token_pc_id
                
            logger.info(f"Auth token validated for PC ID: {pc_id}")
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid authentication token: {e}")
            return False
        
        # Create a new socket.io client
        sio = socketio.AsyncClient(logger=True, engineio_logger=True)
        message_received = asyncio.Event()
        client_update_received = asyncio.Event()
        target_online = False
        
        # Set up event handlers
        @sio.event
        async def connect():
            logger.info("Connected to server")
        
        @sio.event
        async def disconnect():
            logger.info("Disconnected from server")
            if not message_received.is_set():
                logger.warning("Disconnected before receiving acknowledgment")
                message_received.set()  # Ensure we don't hang if disconnected
        
        @sio.event
        async def message(data):
            logger.info(f"Message received: {json.dumps(data, indent=2)}")
            # If this is a response to our config message
            if 'status' in data:
                message_responses['status'] = data.get('status')
                message_responses['message'] = data.get('message', '')
                message_received.set()
                logger.info(f"Acknowledgment received: {data.get('status')} - {data.get('message', '')}")
        
        @sio.event
        async def error(data):
            logger.error(f"Error received: {json.dumps(data, indent=2)}")
            message_responses['error'] = data.get('message', 'Unknown error')
            message_received.set()
            
        @sio.event
        async def clients_update(data):
            nonlocal target_online
            logger.info(f"Clients update received: {data}")
            if pc_id in data:
                logger.info(f"Target PC {pc_id} is connected")
                target_online = True
            else:
                logger.warning(f"Target PC {pc_id} not in connected clients: {data}")
                target_online = False
                
            client_update_received.set()
        
        # Connect to the server
        logger.info(f"Connecting to {SERVER_URL}")
        await sio.connect(SERVER_URL)
        
        # Register with server using a sender ID
        sender_id = f"sender_{uuid.uuid4().hex[:8]}"  # Generate unique sender ID
        logger.info(f"Registering as {sender_id} with token for PC {pc_id}")
        await sio.emit('register', {
            'pc_id': sender_id,
            'auth_token': auth_token
        })
        
        # Wait for registration processing and client list update
        try:
            logger.info("Waiting for client list update")
            await asyncio.wait_for(client_update_received.wait(), timeout=5.0)
            
            if not target_online:
                logger.warning(f"Target PC {pc_id} is not connected to the server")
                logger.info("Continue anyway - target PC might connect later")
        except asyncio.TimeoutError:
            logger.warning("No client list update received; continuing anyway")
        
        # Log info about target PC
        logger.info(f"Preparing to send configuration to PC {pc_id}")
        
        # Send configuration
        logger.info(f"Sending configuration to {pc_id}")
        config_size = len(json.dumps(config))
        logger.info(f"Config size: {config_size} bytes")
        logger.debug(f"Config content sample: {str(config)[:200]}...")
        
        # Send the message and wait for acknowledgment
        logger.info(f"Emitting config message to target {pc_id}")
        await sio.emit('message', {
            'type': 'config',
            'targetId': pc_id,
            'content': config
        })
        
        logger.info(f"Configuration sent to server for delivery to {pc_id}")
        
        # Wait for response with timeout
        try:
            logger.info("Waiting for acknowledgment...")
            await asyncio.wait_for(message_received.wait(), timeout=10.0)
            
            if 'error' in message_responses:
                logger.error(f"Error sending config: {message_responses['error']}")
                return False
            
            if message_responses.get('status') == 'success':
                logger.info(f"Configuration sent successfully: {message_responses.get('message', '')}")
                return True
            else:
                logger.warning(f"Unexpected response: {message_responses}")
                return False
                
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for server response")
            return False
        
    except Exception as e:
        logger.error(f"Error sending config: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False
    finally:
        # Clean disconnect to avoid race conditions
        if sio and sio.connected:
            logger.info("Cleanly disconnecting from server")
            try:
                # Use a new event loop for disconnect to avoid conflicts
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're in a running event loop, use run_until_complete
                    fut = asyncio.ensure_future(sio.disconnect())
                    # Wait just a moment for the server to process
                    fut = asyncio.ensure_future(asyncio.sleep(0.5))
                    await fut
                else:
                    # Otherwise, we're probably in a synchronous context
                    pass
            except Exception as e:
                logger.warning(f"Error during clean disconnect: {e}")

def send_config_sync(config, pc_id, auth_token):
    """
    Synchronous wrapper for send_config
    
    Args:
        config: The configuration to send
        pc_id: The ID of the PC to send the config to
        auth_token: JWT token for authentication
    
    Returns:
        bool: True if configuration was sent successfully, False otherwise
    """
    try:
        # Check if an event loop is already running
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # No event loop is running
            loop = None
            
        if loop and loop.is_running():
            # If an event loop is running, schedule the coroutine
            logger.info("Using existing event loop")
            future = asyncio.run_coroutine_threadsafe(send_config(config, pc_id, auth_token), loop)
            return future.result(timeout=30)  # Wait up to 30 seconds for result
        else:
            # If no event loop is running, use asyncio.run()
            logger.info("Creating new event loop")
            return asyncio.run(send_config(config, pc_id, auth_token))
    except Exception as e:
        logger.error(f"Error in send_config_sync: {str(e)}")
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    # Test code
    try:
        import os
        configpath = os.path.join(os.path.dirname(__file__), "..", 'st_config.json')
        if not os.path.exists(configpath):
            print(f"Config file not found at {configpath}")
            exit(1)
            
        with open(configpath, 'r') as file:
            config = json.load(file)
            
        # Generate a test token for a test PC
        test_pc_id = f"pc_{uuid.uuid4().hex[:6]}"
        test_token = jwt.encode({
            'pc_id': test_pc_id, 
            'name': f"Test PC {test_pc_id}",
            'exp': int(time.time()) + 3600  # 1 hour expiry
        }, JWT_SECRET, algorithm='HS256')
        
        print(f"Testing with PC ID: {test_pc_id}")
        print(f"Test token: {test_token}")
        
        result = send_config_sync(config, test_pc_id, test_token)
        print(f"Send result: {result}")
    except Exception as e:
        print(f"Test error: {e}")
        traceback.print_exc()