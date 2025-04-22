# Mini Operator System
# Implementation using Python and WebSocket

import asyncio
import json
import logging
import websockets
from enum import Enum
from typing import Dict, List, Optional, Union, Any, Callable
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MiniOperator")

# Define message types
class MessageType(Enum):
    COMMAND = "command"
    RESPONSE = "response"
    EVENT = "event"
    ERROR = "error"

# Define operation statuses
class OperationStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class Message:
    """Base message structure for the operator system"""
    type: str
    operation_id: str
    timestamp: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    content: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Convert message to JSON string"""
        return json.dumps(asdict(self))
    
    @classmethod
    def from_json(cls, json_str: str) -> 'Message':
        """Create message from JSON string"""
        data = json.loads(json_str)
        return cls(**data)

@dataclass
class Operation:
    """Represents an operation in the system"""
    id: str
    name: str
    status: OperationStatus = OperationStatus.PENDING
    created_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    updated_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    parameters: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    progress: float = 0.0

    def update_status(self, status: OperationStatus, progress: float = None) -> None:
        """Update operation status and timestamp"""
        self.status = status
        self.updated_at = asyncio.get_event_loop().time()
        if progress is not None:
            self.progress = progress

    def to_dict(self) -> Dict[str, Any]:
        """Convert operation to dictionary"""
        result = {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parameters": self.parameters,
            "progress": self.progress
        }
        if self.result:
            result["result"] = self.result
        if self.error:
            result["error"] = self.error
        return result

class OperationRegistry:
    """Registry for operations and handlers"""
    def __init__(self):
        self.operations: Dict[str, Operation] = {}
        self.handlers: Dict[str, Callable] = {}
    
    def register_handler(self, operation_name: str, handler: Callable) -> None:
        """Register a handler for an operation type"""
        self.handlers[operation_name] = handler
        logger.info(f"Registered handler for operation: {operation_name}")
    
    def add_operation(self, operation: Operation) -> None:
        """Add operation to registry"""
        self.operations[operation.id] = operation
        logger.info(f"Added operation to registry: {operation.id} ({operation.name})")
    
    def get_operation(self, operation_id: str) -> Optional[Operation]:
        """Get operation by ID"""
        return self.operations.get(operation_id)
    
    def get_handler(self, operation_name: str) -> Optional[Callable]:
        """Get handler for operation type"""
        return self.handlers.get(operation_name)

class MiniOperatorServer:
    """Main server class for the Mini Operator System"""
    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.registry = OperationRegistry()
        self.clients = set()
        self.operation_counter = 0
    
    def register_operation_handler(self, operation_name: str, handler: Callable) -> None:
        """Register a handler function for an operation type"""
        self.registry.register_handler(operation_name, handler)
    
    async def broadcast_message(self, message: Message) -> None:
        """Broadcast message to all connected clients"""
        if not self.clients:
            return
        
        json_message = message.to_json()
        await asyncio.gather(
            *[client.send(json_message) for client in self.clients]
        )
    
    async def handle_client(self, websocket, path):
        """Handle client connection"""
        try:
            self.clients.add(websocket)
            logger.info(f"Client connected: {websocket.remote_address}")
            
            async for message_json in websocket:
                try:
                    message = Message.from_json(message_json)
                    await self.process_message(message, websocket)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON received: {message_json}")
                    error_msg = Message(
                        type=MessageType.ERROR.value,
                        operation_id="system",
                        content={"error": "Invalid JSON format"}
                    )
                    await websocket.send(error_msg.to_json())
                except Exception as e:
                    logger.error(f"Error processing message: {str(e)}")
                    error_msg = Message(
                        type=MessageType.ERROR.value,
                        operation_id="system",
                        content={"error": f"Internal error: {str(e)}"}
                    )
                    await websocket.send(error_msg.to_json())
        finally:
            self.clients.remove(websocket)
            logger.info(f"Client disconnected: {websocket.remote_address}")
    
    async def process_message(self, message: Message, client_websocket) -> None:
        """Process incoming message"""
        if message.type != MessageType.COMMAND.value:
            logger.warning(f"Received non-command message: {message.type}")
            return
        
        operation_name = message.content.get("operation")
        if not operation_name:
            error_msg = Message(
                type=MessageType.ERROR.value,
                operation_id=message.operation_id,
                content={"error": "Missing operation name"}
            )
            await client_websocket.send(error_msg.to_json())
            return
        
        handler = self.registry.get_handler(operation_name)
        if not handler:
            error_msg = Message(
                type=MessageType.ERROR.value,
                operation_id=message.operation_id,
                content={"error": f"Unknown operation: {operation_name}"}
            )
            await client_websocket.send(error_msg.to_json())
            return
        
        # Create and register operation
        operation = Operation(
            id=message.operation_id,
            name=operation_name,
            parameters=message.content.get("parameters", {})
        )
        self.registry.add_operation(operation)
        
        # Notify client that operation was received
        ack_msg = Message(
            type=MessageType.RESPONSE.value,
            operation_id=operation.id,
            content={
                "status": OperationStatus.PENDING.value,
                "message": f"Operation {operation_name} received"
            }
        )
        await client_websocket.send(ack_msg.to_json())
        
        # Execute operation in background
        asyncio.create_task(self.execute_operation(operation, handler))
    
    async def execute_operation(self, operation: Operation, handler: Callable) -> None:
        """Execute operation and handle results"""
        try:
            # Update operation status to in-progress
            operation.update_status(OperationStatus.IN_PROGRESS)
            status_msg = Message(
                type=MessageType.EVENT.value,
                operation_id=operation.id,
                content={
                    "status": operation.status.value,
                    "progress": operation.progress
                }
            )
            await self.broadcast_message(status_msg)
            
            # Execute handler
            result = await handler(operation.parameters, self.update_progress_callback(operation))
            
            # Update operation with result
            operation.result = result
            operation.update_status(OperationStatus.COMPLETED, 1.0)
            
            # Send completion message
            complete_msg = Message(
                type=MessageType.RESPONSE.value,
                operation_id=operation.id,
                content={
                    "status": operation.status.value,
                    "result": operation.result,
                    "progress": operation.progress
                }
            )
            await self.broadcast_message(complete_msg)
            
        except Exception as e:
            # Handle operation failure
            logger.error(f"Operation {operation.id} failed: {str(e)}")
            operation.error = str(e)
            operation.update_status(OperationStatus.FAILED)
            
            error_msg = Message(
                type=MessageType.ERROR.value,
                operation_id=operation.id,
                content={
                    "status": operation.status.value,
                    "error": operation.error
                }
            )
            await self.broadcast_message(error_msg)
    
    def update_progress_callback(self, operation: Operation):
        """Create a callback function for updating operation progress"""
        async def update_progress(progress: float, status_message: str = None):
            operation.update_status(OperationStatus.IN_PROGRESS, progress)
            
            progress_msg = Message(
                type=MessageType.EVENT.value,
                operation_id=operation.id,
                content={
                    "status": operation.status.value,
                    "progress": operation.progress,
                    "message": status_message
                }
            )
            await self.broadcast_message(progress_msg)
        
        return update_progress
    
    async def start_server(self):
        """Start the WebSocket server"""
        server = await websockets.serve(self.handle_client, self.host, self.port)
        logger.info(f"Mini Operator System running on {self.host}:{self.port}")
        return server

# Example usage

# Sample operation handlers
async def add_numbers(parameters, progress_callback):
    """Example handler for the 'add' operation"""
    a = parameters.get("a", 0)
    b = parameters.get("b", 0)
    
    # Simulate a long-running operation
    await progress_callback(0.3, "Starting calculation")
    await asyncio.sleep(1)
    
    await progress_callback(0.6, "Processing")
    await asyncio.sleep(1)
    
    # Complete the operation
    result = a + b
    return {"sum": result}

async def echo_message(parameters, progress_callback):
    """Example handler for the 'echo' operation"""
    message = parameters.get("message", "")
    
    await progress_callback(0.5, "Processing message")
    await asyncio.sleep(0.5)
    
    return {"echo": message}

async def heartbeat(parameters, progress_callback):
    vrId = parameters.get("vrId", 0)
    timestamp = parameters.get("timestamp", 0)
    timestamp_s = timestamp / 1000

    # 轉換為 datetime 物件
    dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)

    # 格式化為 ISO 8601 字串
    server_time = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    await progress_callback(0.5, "Processing message")
    await asyncio.sleep(0.5)
    
    return {"status": "success","message": "連線已建立","serverTime": server_time}

async def main():
    # Create and configure the operator server
    server = MiniOperatorServer("0.0.0.0", 8765)
    
    # Register operation handlers
    server.register_operation_handler("add", add_numbers)
    server.register_operation_handler("echo", echo_message)
    server.register_operation_handler("HEARTBEAT", heartbeat)
    
    # Start the server
    await server.start_server()
    
    # Keep the server running
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
