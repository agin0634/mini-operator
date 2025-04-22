# op-server.py
# Simplified WebSocket server based on mini-operator-system.py

import asyncio
import json
import logging
import websockets
from datetime import datetime, timezone, timedelta
from typing import Dict, Callable, Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("OpServer")

class CommandRegistry:
    """Registry for commands and handlers"""
    def __init__(self):
        self.handlers: Dict[str, Callable] = {}
    
    def register_handler(self, command_name: str, handler: Callable) -> None:
        """Register a handler for a command"""
        self.handlers[command_name] = handler
        logger.info(f"Registered handler for command: {command_name}")
    
    def get_handler(self, command_name: str) -> Optional[Callable]:
        """Get handler for command"""
        return self.handlers.get(command_name)

class OpServer:
    """Main server class for the Op Server"""
    def __init__(self, host: str = "localhost", port: int = 8766): # Use a different port
        self.host = host
        self.port = port
        self.registry = CommandRegistry()
        self.clients = set()
    
    def register_command_handler(self, command_name: str, handler: Callable) -> None:
        """Register a handler function for a command"""
        self.registry.register_handler(command_name, handler)
    
    async def broadcast_message(self, message: Dict[str, Any]) -> None:
        """Broadcast message to all connected clients"""
        if not self.clients:
            return
        
        json_message = json.dumps(message)
        # Use asyncio.gather for concurrent sending, handle potential errors
        results = await asyncio.gather(
            *[client.send(json_message) for client in self.clients],
            return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error sending message to a client: {result}")

    async def handle_client(self, websocket, path):
        """Handle client connection"""
        try:
            self.clients.add(websocket)
            logger.info(f"Client connected: {websocket.remote_address}")
            
            async for message_json in websocket:
                try:
                    data = json.loads(message_json)
                    await self.process_command(data, websocket)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON received: {message_json}")
                    error_response = {
                        "status": "error",
                        "message": "Invalid JSON format",
                        "data": {}
                    }
                    await websocket.send(json.dumps(error_response))
                except Exception as e:
                    logger.error(f"Error processing message: {str(e)}", exc_info=True)
                    error_response = {
                        "status": "error",
                        "message": f"Internal server error: {str(e)}",
                        "data": {}
                    }
                    await websocket.send(json.dumps(error_response))
        except websockets.exceptions.ConnectionClosedOK:
             logger.info(f"Client disconnected normally: {websocket.remote_address}")
        except websockets.exceptions.ConnectionClosedError as e:
             logger.warning(f"Client connection closed with error: {websocket.remote_address} - {e}")
        except Exception as e:
            logger.error(f"Unexpected error in handle_client: {e}", exc_info=True)
        finally:
            self.clients.remove(websocket)
            logger.info(f"Client connection closed: {websocket.remote_address}")
    
    async def process_command(self, data: Dict[str, Any], client_websocket) -> None:
        """Process incoming command"""
        command_name = data.get("command")
        if not command_name:
            error_response = {
                "status": "error",
                "message": "Missing 'command' field in request",
                "data": {}
            }
            await client_websocket.send(json.dumps(error_response))
            return
        
        handler = self.registry.get_handler(command_name)
        if not handler:
            error_response = {
                "status": "error",
                "message": f"Unknown command: {command_name}",
                "data": {}
            }
            await client_websocket.send(json.dumps(error_response))
            return
        
        # Execute handler
        try:
            # Pass the entire data dictionary as parameters
            response_data = await handler(data) 
            
            # Ensure the handler returned the expected format
            if not isinstance(response_data, dict) or "status" not in response_data:
                 logger.error(f"Handler for '{command_name}' returned invalid format: {response_data}")
                 response = {
                     "status": "error",
                     "message": f"Internal error: Handler for '{command_name}' did not return expected format.",
                     "data": {}
                 }
            else:
                 response = response_data # Use the handler's response directly

        except Exception as e:
            logger.error(f"Error executing command '{command_name}': {str(e)}", exc_info=True)
            response = {
                "status": "error",
                "message": f"Error executing command '{command_name}': {str(e)}",
                "data": {}
            }
            
        await client_websocket.send(json.dumps(response))

    async def start_server(self):
        """Start the WebSocket server"""
        server = await websockets.serve(self.handle_client, self.host, self.port)
        logger.info(f"OpServer running on ws://{self.host}:{self.port}")
        return server

# --- Example Command Handlers ---

async def ping_handler(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the 'ping' command."""
    logger.info(f"Received ping command with params: {params}")
    return {
        "status": "success",
        "message": "Pong!",
        "data": {"received_params": params} 
    }

async def calculate_sum(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the 'calculate_sum' command."""
    logger.info(f"Received calculate_sum command with params: {params}")
    try:
        num1 = float(params.get("num1", 0))
        num2 = float(params.get("num2", 0))
        total = num1 + num2
        return {
            "status": "success",
            "message": f"Calculation successful.",
            "data": {"sum": total}
        }
    except (ValueError, TypeError) as e:
         logger.error(f"Invalid parameters for calculate_sum: {e}")
         return {
            "status": "error",
            "message": f"Invalid parameters: {e}. Please provide numbers for 'num1' and 'num2'.",
            "data": {}
         }

# --- OP AGENT COMMANDS ---
   
async def heartbeat(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received heartbeat command with params: {params}")
    vrId = params.get("vrId", 0)
    timestamp = float(params.get("timestamp", 0))

    # Define Taipei timezone (UTC+8)
    taipei_tz = timezone(timedelta(hours=8))

    # 轉換為 datetime 物件 using Taipei time
    dt = datetime.fromtimestamp(timestamp)

    # 格式化為 ISO 8601 字串 (with timezone offset)
    server_time = dt.isoformat(timespec='seconds') # Use isoformat() for standard representation including offset
    try:
        #TODO check vrId
        return {
            "status": "success",
            "message": "Connect successful.",
            "data": {"serverTime": server_time}
        }
    except (ValueError, TypeError) as e:
         logger.error(f"Invalid VrID")
         return {
            "status": "error",
            "message": "Connect fail",
            "data": {"serverTime": server_time}
         }
    
async def client_status_update(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received client_status_update command with params: {params}")
    return {
        "status": "success",
        "message": "client_status_update!",
        "data": {"received_params": params}
    }

async def status_update(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received status_update command with params: {params}")
    return {
        "status": "success",
        "message": "status_update!",
        "data": {"received_params": params}
    }

async def user_paired(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received user_paired command with params: {params}")
    return {
        "status": "success",
        "message": "user_paired!",
        "data": {"received_params": params}
    }

async def start_client(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received start_client command with params: {params}")
    return {
        "status": "success",
        "message": "start_client!",
        "data": {"received_params": params}
    }

async def restart_content_client(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received restart_content_client command with params: {params}")
    return {
        "status": "success",
        "message": "restart_content_client!",
        "data": {"received_params": params}
    }

async def close_content_client(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received close_content_client command with params: {params}")
    return {
        "status": "success",
        "message": "close_content_client!",
        "data": {"received_params": params}
    }
    
# --- OP FRONTEND COMMANDS ---
async def get_system_status(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received get_system_status command with params: {params}")

    #TODO
    return {
        "status": "success",
        "message": "system status!",
        "data": {"received_params": params}
    }
    
async def create_room(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received create_room command with params: {params}")
    return {
        "status": "success",
        "message": "create_room!",
        "data": {"received_params": params}
    }

async def pair_user(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received pair_user command with params: {params}")
    return {
        "status": "success",
        "message": "pair_user!",
        "data": {"received_params": params}
    }

async def set_content_language(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received set_content_language command with params: {params}")
    return {
        "status": "success",
        "message": "set_content_language!",
        "data": {"received_params": params}
    }

async def start_content(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received start_content command with params: {params}")
    return {
        "status": "success",
        "message": "start_content!",
        "data": {"received_params": params}
    }

async def change_content_status(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received change_content_status command with params: {params}")
    return {
        "status": "success",
        "message": "change_content_status!",
        "data": {"received_params": params}
    }

async def close_content(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received close_content command with params: {params}")
    return {
        "status": "success",
        "message": "close_content!",
        "data": {"received_params": params}
    }

async def release_room(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received release_room command with params: {params}")
    return {
        "status": "success",
        "message": "release_room!",
        "data": {"received_params": params}
    }

async def room_status_update(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received room_status_update command with params: {params}")
    return {
        "status": "success",
        "message": "room_status_update!",
        "data": {"received_params": params}
    }


# --- CONTENT SERVER COMMANDS ---
async def content_server_hello(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received content_server_hello command with params: {params}")

    #TODO
    return {
        "status": "success",
        "message": "content server hello!",
        "data": {"received_params": params}
    }

async def content_server_update(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received content_server_update command with params: {params}")

    #TODO
    return {
        "status": "success",
        "message": "content server update!",
        "data": {"received_params": params}
    }

async def set_content_status(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received set_content_status command with params: {params}")

    #TODO
    return {
        "status": "success",
        "message": "set content status!",
        "data": {"received_params": params}
    }

async def get_user_pairings(params: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Received get_user_pairings command with params: {params}")

    #TODO
    return {
        "status": "success",
        "message": "get user pairings!",
        "data": {"received_params": params}
    }

async def user_pairing_updated(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the USER_PAIRING_UPDATED command from the agent."""
    logger.info(f"Received USER_PAIRING_UPDATED command with params: {params}")
    # TODO: Implement actual logic for handling pairing updates
    return {
        "status": "success",
        "message": "User pairing update received.",
        "data": {"received_params": params}
    }

async def content_language_settings(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the CONTENT_LANGUAGE_SETTINGS command from the agent."""
    logger.info(f"Received CONTENT_LANGUAGE_SETTINGS command with params: {params}")
    # TODO: Implement actual logic for handling language settings
    return {
        "status": "success",
        "message": "Content language settings received.",
        "data": {"received_params": params}
    }

async def close_content_server(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the CLOSE_CONTENT_SERVER command from the agent."""
    logger.info(f"Received CLOSE_CONTENT_SERVER command with params: {params}")
    # TODO: Implement actual logic for closing the content server connection/process
    return {
        "status": "success",
        "message": "Close content server command received.",
        "data": {"received_params": params}
    }

# --- Main Execution ---

async def main():
    # Create and configure the server
    server = OpServer("0.0.0.0", 8766) # Listen on all interfaces, port 8766
    
    # Register command handlers
    # sample command
    server.register_command_handler("PING", ping_handler)
    server.register_command_handler("CALCULATE_SUM", calculate_sum)

    # op agent commands
    server.register_command_handler("HEARTBEAT", heartbeat)
    server.register_command_handler("CLIENT_STATUS_UPDATE", client_status_update)
    server.register_command_handler("STATUS_UPDATE", status_update)
    server.register_command_handler("USER_PAIRED", user_paired)
    server.register_command_handler("START_CLIENT", start_client)
    server.register_command_handler("RESTART_CONTENT_CLIENT", restart_content_client)
    server.register_command_handler("CLOSE_CONTENT_CLIENT", close_content_client)

    # frontend commands
    server.register_command_handler("GET_SYSTEM_STATUS", get_system_status)
    server.register_command_handler("CREATE_ROOM", create_room)
    server.register_command_handler("PAIR_USER", pair_user)
    server.register_command_handler("SET_CONTENT_LANGUAGE", set_content_language)
    server.register_command_handler("START_CONTENT", start_content)
    server.register_command_handler("CHANGE_CONTENT_STATUS", change_content_status)
    server.register_command_handler("RESTART_CONTENT_CLIENT", restart_content_client)
    server.register_command_handler("CLOSE_CONTENT", close_content)
    server.register_command_handler("RELEASE_ROOM", release_room)
    server.register_command_handler("ROOM_STATUS_UPDATE", room_status_update)

    # content server commands
    server.register_command_handler("CONTENT_SERVER_HELLO", content_server_hello)
    server.register_command_handler("CONTENT_SERVER_UPDATE", content_server_update)
    server.register_command_handler("SET_CONTENT_STATUS", set_content_status)
    server.register_command_handler("GET_USER_PAIRINGS", get_user_pairings)
    server.register_command_handler("USER_PAIRING_UPDATED", user_pairing_updated)
    server.register_command_handler("CONTENT_LANGUAGE_SETTINGS", content_language_settings)
    server.register_command_handler("CLOSE_CONTENT_SERVER", close_content_server)
    
    # Start the server
    await server.start_server()
    
    # Keep the server running indefinitely
    await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
