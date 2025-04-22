# Mini Operator System Client
# Client implementation to interact with the operator system

import asyncio
import json
import logging
import sys
import uuid
import websockets
from typing import Dict, Any, Optional, Callable

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("OperatorClient")

class OperatorClient:
    def __init__(self, server_url: str = "ws://localhost:8765"):
        self.server_url = server_url
        self.websocket = None
        self.callbacks = {}
        self.connected = False
    
    async def connect(self):
        """Connect to the operator server"""
        try:
            self.websocket = await websockets.connect(self.server_url)
            self.connected = True
            logger.info(f"Connected to operator server at {self.server_url}")
            
            # Start message listener in background
            asyncio.create_task(self._message_listener())
            return True
        except Exception as e:
            logger.error(f"Failed to connect to server: {str(e)}")
            return False
    
    async def _message_listener(self):
        """Listen for messages from the server"""
        try:
            while self.connected and self.websocket:
                message_json = await self.websocket.recv()
                try:
                    message = json.loads(message_json)
                    await self._handle_message(message)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON received: {message_json}")
                except Exception as e:
                    logger.error(f"Error handling message: {str(e)}")
        except websockets.exceptions.ConnectionClosed:
            logger.info("Connection to server closed")
            self.connected = False
        except Exception as e:
            logger.error(f"Error in message listener: {str(e)}")
            self.connected = False
    
    async def _handle_message(self, message: Dict[str, Any]):
        """Process incoming message from the server"""
        operation_id = message.get("operation_id")
        if not operation_id:
            logger.warning("Received message without operation_id")
            return
        
        # Check if there's a callback registered for this operation
        callback = self.callbacks.get(operation_id)
        if callback:
            await callback(message)
        else:
            # Log message if no callback is registered
            logger.info(f"Received message for operation {operation_id}: {message}")
    
    async def send_command(self, operation: str, parameters: Dict[str, Any], 
                           callback: Optional[Callable] = None) -> str:
        """Send a command to the operator server"""
        if not self.connected or not self.websocket:
            raise ConnectionError("Not connected to server")
        
        # Generate a unique operation ID
        operation_id = str(uuid.uuid4())
        
        # Create command message
        command = {
            "type": "command",
            "operation_id": operation_id,
            "timestamp": asyncio.get_event_loop().time(),
            "content": {
                "operation": operation,
                "parameters": parameters
            }
        }
        
        # Register callback if provided
        if callback:
            self.callbacks[operation_id] = callback
        
        # Send the command
        await self.websocket.send(json.dumps(command))
        logger.info(f"Sent command: {operation} with ID {operation_id}")
        
        return operation_id
    
    async def close(self):
        """Close the connection to the server"""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            logger.info("Disconnected from server")

async def progress_callback(message):
    """Example callback to handle operation progress updates"""
    msg_type = message.get("type")
    content = message.get("content", {})
    operation_id = message.get("operation_id")
    
    if msg_type == "event":
        progress = content.get("progress", 0)
        status = content.get("status")
        message_text = content.get("message", "")
        progress_bar = "=" * int(progress * 20) + " " * (20 - int(progress * 20))
        
        print(f"\rOperation {operation_id} [{progress_bar}] {progress*100:.1f}% - {status} {message_text}", end="")
    
    elif msg_type == "response":
        status = content.get("status")
        result = content.get("result")
        print(f"\nOperation {operation_id} completed with status: {status}")
        if result:
            print(f"Result: {json.dumps(result, indent=2)}")
    
    elif msg_type == "error":
        error = content.get("error")
        print(f"\nOperation {operation_id} failed: {error}")

async def main():
    # Parse command line arguments
    if len(sys.argv) < 2:
        print("Usage: python client.py <operation> [parameters]")
        print("Examples:")
        print("  python client.py add a=5 b=3")
        print("  python client.py echo message='Hello, world!'")
        return
    
    operation = sys.argv[1]
    parameters = {}
    
    # Parse parameters from command line
    for arg in sys.argv[2:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            # Try to convert numeric values
            try:
                if '.' in value:
                    parameters[key] = float(value)
                else:
                    parameters[key] = int(value)
            except ValueError:
                parameters[key] = value
    
    # Create client and connect to server
    client = OperatorClient()
    if not await client.connect():
        print("Failed to connect to server")
        return
    
    try:
        # Send the operation command
        operation_id = await client.send_command(operation, parameters, progress_callback)
        print(f"Operation {operation_id} sent. Waiting for results...")
        
        # Wait for operation to complete
        while client.connected:
            await asyncio.sleep(0.1)
    
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
