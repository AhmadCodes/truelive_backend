import asyncio
import websockets

async def receive_message(websocket):
    """
    Handle incoming WebSocket connections and messages.
    Displays any message received from the client.
    """
    client_id = id(websocket)
    print(f"Client {client_id} connected")
    
    try:
        async for message in websocket:
            print(f"Received message from client {client_id}: \n\n {message}\n\n\n")
            
            # Optional: Send acknowledgment back to client
            await websocket.send(f"Message received: {message}")
            
    except websockets.exceptions.ConnectionClosed:
        print(f"Client {client_id} disconnected")
    except Exception as e:
        print(f"Error handling client {client_id}: {str(e)}")

async def main():
    # Start WebSocket server
    async with websockets.serve(receive_message, "localhost", 9002):
        print("WebSocket server started on ws://localhost:9002")
        # Keep the server running indefinitely
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        print(f"Server error: {str(e)}")