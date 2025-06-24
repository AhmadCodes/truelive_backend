import socket
import time

def listen():
    # Create a TCP/IP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    # Bind the socket to the port
    server_address = ('localhost', 9022)
    print(f"Starting up listener on {server_address[0]}:{server_address[1]}")
    
    try:
        sock.bind(server_address)
        
        # Listen for incoming connections
        sock.listen(1)
        
        while True:
            # Wait for a connection
            print("Waiting for a message...")
            connection, client_address = sock.accept()
            
            try:
                # Receive data in small chunks
                while True:
                    data = connection.recv(1024)
                    if data:
                        print(f"Received message: {data!r}")
                    else:
                        break
                    
            finally:
                connection.close()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        print("Closing socket")
        sock.close()

if __name__ == "__main__":
    while True:
        try:
            listen()
        except KeyboardInterrupt:
            print("\nExiting program")
            break
        except Exception as e:
            print(f"Error occurred: {e}")
            print("Reconnecting in 3 seconds...")
            time.sleep(3)