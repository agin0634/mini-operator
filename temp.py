#!/usr/bin/env python3
# op-server-gui.py
# WebSocket server with PyQt GUI interface

import asyncio
import json
import logging
import websockets
import sys
import socket
from datetime import datetime, timezone, timedelta
from typing import Dict, Callable, Any, Optional, Set
from dark_theme import dark_stylesheet

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                           QTextEdit, QSpinBox, QGroupBox, QStatusBar, 
                           QComboBox, QMessageBox)
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QThread, QTimer
from PyQt5.QtGui import QFont, QTextCursor

# 全域變數
connected_clients = []
agents = []
rooms = []

# Custom logging handler that emits signals for log messages
class QTextEditLogger(logging.Handler, QObject):
    log_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        logging.Handler.__init__(self)
        QObject.__init__(self, parent)
        self.widget = QTextEdit(parent)
        self.widget.setReadOnly(True)
        self.widget.setFont(QFont("Courier", 10))
        self.log_signal.connect(self.append_log)
        
        # Create a formatter
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.setFormatter(formatter)

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg)

    @pyqtSlot(str)
    def append_log(self, message):
        self.widget.append(message)
        # Auto-scroll to bottom
        cursor = self.widget.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.widget.setTextCursor(cursor)

class CommandRegistry:
    """Registry for commands and handlers"""
    def __init__(self):
        self.handlers: Dict[str, Callable] = {}
    
    def register_handler(self, command_name: str, handler: Callable) -> None:
        """Register a handler for a command"""
        self.handlers[command_name] = handler
        logging.info(f"Registered handler for command: {command_name}")
    
    def get_handler(self, command_name: str) -> Optional[Callable]:
        """Get handler for command"""
        return self.handlers.get(command_name)

class WebSocketServerThread(QThread):
    # Signals
    server_status = pyqtSignal(bool, str)
    client_connected = pyqtSignal(str)
    client_disconnected = pyqtSignal(str)
    
    def __init__(self, host, port):
        super().__init__()
        self.host = host
        self.port = port
        self.is_running = False
        self.server = None
        self.registry = CommandRegistry()
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.vr_clients: Dict[str, websockets.WebSocketServerProtocol] = {} # Map vrId to websocket
    
    def register_command_handler(self, command_name: str, handler: Callable) -> None:
        """Register a handler function for a command"""
        self.registry.register_handler(command_name, handler)

    async def handle_client(self, websocket, path):
        """Handle client connection"""
        try:
            self.clients.add(websocket)
            client_info = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
            logging.info(f"Client connected: {client_info}")
            self.client_connected.emit(client_info)
            
            async for message_json in websocket:
                try:
                    data = json.loads(message_json)
                    await self.process_command(data, websocket)
                except json.JSONDecodeError:
                    logging.error(f"Invalid JSON received: {message_json}")
                    error_response = {
                        "status": "error",
                        "message": "Invalid JSON format",
                        "data": {}
                    }
                    await websocket.send(json.dumps(error_response))
                except Exception as e:
                    logging.error(f"Error processing message: {str(e)}", exc_info=True)
                    error_response = {
                        "status": "error",
                        "message": f"Internal server error: {str(e)}",
                        "data": {}
                    }
                    await websocket.send(json.dumps(error_response))
        except websockets.exceptions.ConnectionClosedOK:
            logging.info(f"Client disconnected normally: {websocket.remote_address}")
        except websockets.exceptions.ConnectionClosedError as e:
            logging.warning(f"Client connection closed with error: {websocket.remote_address} - {e}")
        except Exception as e:
            logging.error(f"Unexpected error in handle_client: {e}", exc_info=True)
        finally:
            self.clients.remove(websocket)
            client_info = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
            logging.info(f"Client connection closed: {client_info}")
            # Remove client from vr_clients map if present
            vr_id_to_remove = None
            for vr_id, ws in self.vr_clients.items():
                if ws == websocket:
                    vr_id_to_remove = vr_id
                    break
            if vr_id_to_remove:
                del self.vr_clients[vr_id_to_remove]
                logging.info(f"Removed vrId {vr_id_to_remove} from client map.")

            self.client_disconnected.emit(client_info)


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
            # Pass the server thread instance and the client websocket to the handler
            response_data = await handler(self, client_websocket, data) 
            
            # Ensure the handler returned the expected format
            if not isinstance(response_data, dict) or "status" not in response_data:
                logging.error(f"Handler for '{command_name}' returned invalid format: {response_data}")
                response = {
                    "status": "error",
                    "message": f"Internal error: Handler for '{command_name}' did not return expected format.",
                    "data": {}
                }
            else:
                response = response_data  # Use the handler's response directly

        except Exception as e:
            logging.error(f"Error executing command '{command_name}': {str(e)}", exc_info=True)
            response = {
                "status": "error",
                "message": f"Error executing command '{command_name}': {str(e)}",
                "data": {}
            }
            
        await client_websocket.send(json.dumps(response))

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
                logging.error(f"Error sending message to a client: {result}")

    async def start_server(self):
        """Start the WebSocket server"""
        try:
            self.server = await websockets.serve(self.handle_client, self.host, self.port)
            logging.info(f"Server running on ws://{self.host}:{self.port}")
            self.server_status.emit(True, f"Running on {self.host}:{self.port}")
            return self.server
        except Exception as e:
            logging.error(f"Failed to start server: {e}")
            self.server_status.emit(False, f"Error: {str(e)}")
            return None

    async def stop_server(self):
        """Stop the WebSocket server"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            logging.info("Server stopped")
        self.is_running = False

    async def run_async(self):
        """Main async entry point"""
        self.is_running = True
        
        # Start the server
        if await self.start_server():
            # Wait until stopped
            while self.is_running:
                await asyncio.sleep(0.1)
        
        # Clean up
        await self.stop_server()

    def run(self):
        """QThread entry point"""
        asyncio.run(self.run_async())
    
    def stop(self):
        """Stop the thread"""
        self.is_running = False

    async def send_to_client(self, vr_id: str, message: Dict[str, Any]):
        """Send a JSON message to a specific client by vrId."""
        websocket = self.vr_clients.get(vr_id)
        if websocket:
            try:
                await websocket.send(json.dumps(message))
                logging.info(f"Sent message to {vr_id}: {message}")
            except websockets.exceptions.ConnectionClosed:
                logging.warning(f"Could not send message to {vr_id}: Connection closed.")
                # Optionally remove from map here as well
                if vr_id in self.vr_clients:
                    del self.vr_clients[vr_id]
            except Exception as e:
                logging.error(f"Error sending message to {vr_id}: {e}")
        else:
            logging.warning(f"Could not send message: vrId {vr_id} not found in connected clients.")

    # OP SERVER TO CONTENT SERVER
    @staticmethod
    async def start_content_server(self, port: str):
        """Placeholder for starting content server"""
        # This likely needs more complex logic, potentially running a subprocess
        # or interacting with another system. For now, just log.
        logging.info(f"Placeholder: Would start content server process for room {port}")
        # Example: In a real scenario, you might run:
        # subprocess.Popen(["python", "content_server.py", "--port", port])
        return

# --- Command Handlers ---
# Note: All handlers now accept server_thread and client_websocket as first two args
async def heartbeat(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received heartbeat command with params: {params}")
    vrId = str(params.get("vrId", 0))
    timestamp = float(params.get("timestamp", 0))

    #taipei_tz = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(timestamp)

    # 格式化為 ISO 8601 字串 (with timezone offset)
    server_time = dt.isoformat(timespec='seconds')
    if not vrId:
        return {"status": "error", "message": "Missing vrId in heartbeat", "serverTime": server_time}

    # Update vr_clients map
    server_thread.vr_clients[vrId] = client_websocket
    logging.info(f"Updated vr_clients map for {vrId}. Current map size: {len(server_thread.vr_clients)}")

    try:
        agent_found = False
        for agent in agents:
            if agent["vrId"] == vrId:
                agent["connectionStatus"] = "Connected"
                agent_found = True
                logging.info(f"Agent {vrId} reconnected.")
                return {
                    "status": "success",
                    "message": "Reconnect successful",
                    "serverTime": server_time
                }
        
        if not agent_found:
            new_agent = {
            "vrId": vrId,
            "connectionStatus": "Connected",
            "currentContent": "無",
            "assignedRoom": "",
                "clientState": "idle"
            }
            agents.append(new_agent)
            logging.info(f"New agent {vrId} connected. Current agents: {agents}")

        return {
            "status": "success",
            "message": "Connect successful.",
            "serverTime": server_time
        }
    except Exception as e: # Catch broader exceptions
        logging.error(f"Error processing heartbeat for {vrId}: {e}", exc_info=True)
        return {
           "status": "error",
           "message": f"Error processing heartbeat: {e}",
           "serverTime": server_time
        }

async def client_status_update(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    # Example: Update agent state based on client report
    vrId = params.get("vrId")
    clientState = params.get("clientState")
    currentContent = params.get("currentContent")
    logging.info(f"Received client_status_update from {vrId}: {params}")

    agent_updated = False
    if vrId:
        for agent in agents:
            if agent["vrId"] == vrId:
                if clientState:
                    agent["clientState"] = clientState
                if currentContent:
                    agent["currentContent"] = currentContent
                agent_updated = True
                logging.info(f"Updated agent {vrId} status: {agent}")
                break

    if not agent_updated:
         logging.warning(f"Received status update for unknown or missing vrId: {vrId}")
         # Optionally return an error if vrId is expected but missing/not found
         # return {"status": "error", "message": f"Agent {vrId} not found"}

    return {
        "status": "success", # Acknowledge receipt even if agent wasn't found, unless error is desired
        "message": "Client status update received.",
        "data": {"received_params": params}
    }

# --- OP FRONTEND COMMANDS ---
async def get_system_status(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received get_system_status command with params: {params}")

    # Add connection status from the live map
    live_vr_ids = set(server_thread.vr_clients.keys())
    for agent in agents:
        if agent["vrId"] in live_vr_ids:
             agent["connectionStatus"] = "Connected"
        else:
             # If agent is in list but not in live map, mark as disconnected
             agent["connectionStatus"] = "Disconnected"

    result = {
    "status": "success",
    "agents": agents,
    "rooms": rooms
    }
    # print(result) # Avoid printing large structures to console in production

    return result

async def create_room(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received create_room command with params: {params}")
    vrIds_param = params.get("vrIds", []) # Expecting a list
    language = str(params.get("language", "EN")) # Default language
    contentName = str(params.get("contentName", "Default Content")) # Example: Allow specifying content name
    contentId = str(params.get("contentId", "DefaultContent001")) # Example: Allow specifying content ID

    # Ensure vrIds is a list of strings
    if isinstance(vrIds_param, str):
        vrIds = [v.strip() for v in vrIds_param.split(',') if v.strip()]
    elif isinstance(vrIds_param, list):
        vrIds = [str(v).strip() for v in vrIds_param if str(v).strip()]
    else:
        return {"status": "error", "message": "Invalid format for vrIds. Expected list or comma-separated string."}

    if not vrIds:
        return {"status": "error", "message": "No valid vrIds provided for room creation."}

    # Generate a unique room ID (simple example)
    new_room_id = f"R{len(rooms) + 8001}"
    server_time = datetime.now(timezone.utc).isoformat(timespec='seconds')

    try:
        new_room = {
            "roomId": new_room_id,
            "status": "Ready",
            "contentName": contentName,
            "contentId": contentId,
            "startTime": server_time, # Consider setting startTime only when content starts
            "assignedVRs": vrIds, # Store the requested VR IDs
            "users": [] # Users with tokens and language settings
        }

        # Update agent states for those assigned to the new room
        assigned_agents_count = 0
        for vrId in vrIds:
            agent_found = False
            for agent in agents:
                if agent["vrId"] == vrId:
                    # Only assign if agent is idle or already connected
                    if agent.get("clientState") == "idle" or agent.get("connectionStatus") == "Connected":
                         agent["assignedRoom"] = new_room_id
                         agent["clientState"] = "in_room"
                         # Add user entry for this VR in the room
                         new_user = {
                             "vrId": vrId,
                             "userToken": "", # Token added later via PAIR_USER
                             "language": language
                         }
                         new_room["users"].append(new_user)
                         assigned_agents_count += 1
                         agent_found = True
                         logging.info(f"Assigned agent {vrId} to room {new_room_id}")
                         break
                    else:
                         logging.warning(f"Agent {vrId} is not idle ({agent.get('clientState')}), cannot assign to room {new_room_id}.")
                         agent_found = True # Found but couldn't assign
                         break
            if not agent_found:
                 logging.warning(f"Agent {vrId} not found in agent list, cannot assign to room {new_room_id}.")
                 # Optionally, decide if room creation should fail if agents aren't found/ready

        if assigned_agents_count == 0 and len(vrIds) > 0:
             logging.warning(f"Room {new_room_id} created, but no agents could be assigned.")
             # Decide if this is an error or just a warning

        rooms.append(new_room)
        logging.info(f"Room {new_room_id} created: {new_room}")

        # TODO: Optionally notify the assigned clients that they are now in a room?

        return {
            "status": "success",
            "message": f"Room {new_room_id} created.",
            "roomId": new_room_id,
            "assignedVRs": vrIds # Return the list of initially requested VRs
        }
    except Exception as e: # Catch broader exceptions
        logging.error(f"Error creating room: {e}", exc_info=True)
        return {
           "status": "error",
           "message": f"Failed to create room: {e}",
           "serverTime": server_time
        }

async def pair_user(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received pair_user command with params: {params}")
    roomId = str(params.get("roomId", ""))
    vrId = str(params.get("vrId", ""))
    userToken = str(params.get("userToken", ""))

    if not roomId or not vrId or not userToken:
        return {"status": "error", "message": "Missing roomId, vrId, or userToken for pairing."}

    paired = False
    try:
        target_room = None
        for room in rooms:
            if room["roomId"] == roomId:
                target_room = room
                break

        if not target_room:
            logging.error(f"Pairing failed: Room {roomId} not found.")
            return {"status": "error", "message": f"Room {roomId} not found."}

        user_found = False
        for user in target_room["users"]:
            if user["vrId"] == vrId:
                user["userToken"] = userToken
                user_found = True
                paired = True
                logging.info(f"User paired in room {roomId}: vrId={vrId}, token={userToken}")
                break

        if not user_found:
             # If user wasn't pre-added during room creation, maybe add them now? Or error?
             # Current logic assumes user entry exists from create_room.
             logging.error(f"Pairing failed: VR ID {vrId} not found in room {roomId}'s user list.")
             return {"status": "error", "message": f"VR ID {vrId} not assigned to room {roomId}."}

        # TODO: Notify the specific client (vrId) about the pairing?
        # await server_thread.send_to_client(vrId, {"command": "USER_PAIRED", "userToken": userToken, "roomId": roomId})

        return {
            "status": "success",
            "message": "User paired successfully",
            "roomId": roomId,
            "vrId": vrId,
            "userToken": userToken
        }

    except Exception as e:
        logging.error(f"Error during pairing user {vrId} in room {roomId}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error pairing user: {e}",
        }

async def set_content_language(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received set_content_language command with params: {params}")
    vrId = str(params.get("vrId", ""))
    roomId = str(params.get("roomId", ""))
    language = str(params.get("language", ""))

    if not roomId or not vrId or not language:
        return {"status": "error", "message": "Missing roomId, vrId, or language."}

    language_set = False
    try:
        target_room = None
        for r in rooms:
            if r["roomId"] == roomId:
                target_room = r
                break

        if not target_room:
            logging.error(f"Set language failed: Room {roomId} not found.")
            return {"status": "error", "message": f"Room {roomId} not found."}

        user_found = False
        for user in target_room["users"]:
            if user["vrId"] == vrId:
                user["language"] = language
                user_found = True
                language_set = True
                logging.info(f"Language set for user {vrId} in room {roomId} to {language}")
                break

        if not user_found:
             logging.error(f"Set language failed: VR ID {vrId} not found in room {roomId}'s user list.")
             return {"status": "error", "message": f"VR ID {vrId} not assigned to room {roomId}."}

        # Notify the specific client about the language change
        await server_thread.send_to_client(vrId, {"command": "SET_LANGUAGE", "language": language})

        return {
            "status": "success",
            "vrId": vrId,
            "language": language,
            "message": "Language set successfully and client notified."
        }

    except Exception as e:
        logging.error(f"Error setting language for {vrId} in room {roomId}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error setting language: {e}"
        }


async def start_content(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received start_content command with params: {params}")
    roomId = str(params.get("roomId", ""))
    contentId = str(params.get("contentId", "")) # Use provided contentId

    if not roomId:
        return {"status": "error", "message": "Missing roomId."}
    # Allow starting without a specific contentId if room already has one? Or require it?
    # if not contentId:
    #     return {"status": "error", "message": "Missing contentId."}

    start_time = datetime.now(timezone.utc).isoformat(timespec='seconds')
    notifications_sent = 0
    try:
        target_room = None
        for r in rooms:
            if r["roomId"] == roomId:
                target_room = r
                break

        if not target_room:
            logging.error(f"Start content failed: Room {roomId} not found.")
            return {"status": "error", "message": f"Room {roomId} not found."}

        # Update room status and potentially contentId if provided
        target_room["status"] = "Playing"
        target_room["startTime"] = start_time # Record start time
        if contentId: # Update contentId if a new one was given
             target_room["contentId"] = contentId
        final_content_id = target_room["contentId"] # Use the room's current contentId

        logging.info(f"Starting content {final_content_id} in room {roomId}. Notifying clients...")

        # Notify assigned clients to start the content
        for user in target_room["users"]:
            vrId = user["vrId"]
            language = user["language"]
            userToken = user["userToken"]
            start_command = {
                "command": "START_CLIENT_CONTENT",
                "contentId": final_content_id,
                "roomId": roomId,
                "language": language,
                "userToken": userToken # Send token if available
            }
            await server_thread.send_to_client(vrId, start_command)
            notifications_sent += 1

        # Placeholder for notifying/starting a dedicated content server process if needed
        # await server_thread.start_content_server(roomId)

        logging.info(f"Sent {notifications_sent} start notifications for room {roomId}.")

        return {
            "status": "success",
            "roomId": roomId,
            "contentId": final_content_id,
            "message": f"Content {final_content_id} started successfully in room {roomId}. {notifications_sent} clients notified."
        }

    except Exception as e:
        logging.error(f"Error starting content in room {roomId}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error starting content: {e}"
        }


async def change_content_status(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received change_content_status command with params: {params}")
    roomId = str(params.get("roomId", ""))
    new_status = str(params.get("status", "")) # e.g., "Paused", "Playing", "End"

    if not roomId or not new_status:
        return {"status": "error", "message": "Missing roomId or status."}

    notifications_sent = 0
    try:
        target_room = None
        for r in rooms:
            if r["roomId"] == roomId:
                target_room = r
                break

        if not target_room:
            logging.error(f"Change status failed: Room {roomId} not found.")
            return {"status": "error", "message": f"Room {roomId} not found."}

        target_room["status"] = new_status
        logging.info(f"Room {roomId} status changed to {new_status}. Notifying clients...")

        # Notify clients in the room about the status change
        status_command = {
            "command": "SET_CLIENT_STATUS",
            "roomId": roomId,
            "status": new_status
        }
        for user in target_room["users"]:
            await server_thread.send_to_client(user["vrId"], status_command)
            notifications_sent += 1

        logging.info(f"Sent {notifications_sent} status change notifications for room {roomId}.")

        return {
            "status": "success",
            "roomId": roomId,
            "newStatus": new_status,
            "message": f"Content status changed to {new_status} successfully. {notifications_sent} clients notified."
        }
    except Exception as e:
        logging.error(f"Error changing status for room {roomId}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error changing status: {e}"
        }


async def restart_content_client(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received restart_content_client command with params: {params}")
    vrId = str(params.get("vrId", ""))
    # contentId = str(params.get("contentId", "")) # Content ID might be needed by client
    roomId = str(params.get("roomId", "")) # Room context might be useful

    if not vrId:
        return {"status": "error", "message": "Missing vrId."}

    # Find the client and send restart command
    logging.info(f"Attempting to send restart command to client {vrId} (context room: {roomId})")
    restart_command = {
        "command": "RESTART_CLIENT",
        # "contentId": contentId, # Include if client needs it to restart specific content
        "roomId": roomId # Include room context if needed
    }
    await server_thread.send_to_client(vrId, restart_command)

    # Note: We send the command even if the vrId isn't found in a specific room's user list,
    # as the command is targeted directly at the vrId.
    # The send_to_client method handles logging if the client isn't connected.

    # Update agent state if found
    for agent in agents:
        if agent["vrId"] == vrId:
            agent["clientState"] = "restarting" # Or some appropriate state
            logging.info(f"Marked agent {vrId} state as restarting.")
            break

    return {
        "status": "success",
        "vrId": vrId,
        "message": f"Restart command sent to client {vrId}."
    }
    # No try/except needed here as send_to_client handles errors


async def close_content(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received close_content command with params: {params}")
    roomId = str(params.get("roomId", ""))

    if not roomId:
        return {"status": "error", "message": "Missing roomId."}

    notifications_sent = 0
    try:
        target_room = None
        room_index = -1
        for i, r in enumerate(rooms):
            if r["roomId"] == roomId:
                target_room = r
                room_index = i
                break

        if not target_room:
            logging.error(f"Close content failed: Room {roomId} not found.")
            return {"status": "error", "message": f"Room {roomId} not found."}

        target_room["status"] = "End"
        logging.info(f"Closing content in room {roomId}. Notifying clients...")

        # Notify clients in the room to close/exit content
        close_command = {
            "command": "CLOSE_CLIENT_CONTENT",
            "roomId": roomId
        }
        vrIds_in_room = [user["vrId"] for user in target_room["users"]]
        for vrId in vrIds_in_room:
            await server_thread.send_to_client(vrId, close_command)
            notifications_sent += 1
            # Update agent state to idle after closing content
            for agent in agents:
                 if agent["vrId"] == vrId:
                      agent["clientState"] = "idle"
                      agent["assignedRoom"] = "" # Clear assigned room
                      logging.info(f"Set agent {vrId} state to idle after closing content.")
                      break

        logging.info(f"Sent {notifications_sent} close notifications for room {roomId}.")

        # Optionally: Notify a content server process to shut down

        # Consider if the room should be removed immediately after closing content,
        # or if RELEASE_ROOM is a separate step. Current logic keeps the room until RELEASE_ROOM.
        # If removing here:
        # if room_index != -1:
        #     del rooms[room_index]
        #     logging.info(f"Room {roomId} removed after closing content.")

        return {
            "status": "success",
            "roomId": roomId,
            "message": f"Content closed successfully in room {roomId}. {notifications_sent} clients notified."
        }
    except Exception as e:
        logging.error(f"Error closing content for room {roomId}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error closing content: {e}"
        }


async def release_room(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received release_room command with params: {params}")
    roomId = str(params.get("roomId", ""))

    if not roomId:
        return {"status": "error", "message": "Missing roomId."}

    room_found = False
    try:
        room_to_remove = None
        vrIds_to_release = []
        for room in rooms:
            if room["roomId"] == roomId:
                room_to_remove = room
                vrIds_to_release = [user["vrId"] for user in room["users"]]
                room_found = True
                break

        if room_to_remove:
            rooms.remove(room_to_remove)
            logging.info(f"Room {roomId} released and removed.")

            # Set agents previously in this room back to idle
            for vrId in vrIds_to_release:
                 for agent in agents:
                      if agent["vrId"] == vrId and agent.get("assignedRoom") == roomId:
                           agent["clientState"] = "idle"
                           agent["assignedRoom"] = ""
                           logging.info(f"Set agent {vrId} state to idle after releasing room {roomId}.")
                           # Optionally send a notification to the client?
                           # await server_thread.send_to_client(vrId, {"command": "ROOM_RELEASED"})
                           break

            return {
                "status": "success",
                "message": f"Room {roomId} released successfully.",
                "roomId": roomId,
            }
        else:
            logging.warning(f"Release room failed: Room {roomId} not found.")
            return {
                "status": "error",
                "message": f"Room {roomId} not found, cannot release.",
            }
    except Exception as e:
        logging.error(f"Error releasing room {roomId}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error releasing room: {e}"
        }


# --- CONTENT SERVER COMMANDS ---
# These handlers likely represent commands *sent by* a separate content server process,
# not commands initiated by the frontend. They update the central state (rooms).

async def content_server_hello(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    # This command might be used by a content server process to register itself for a room
    logging.info(f"Received content_server_hello command: {params}")
    contentId = str(params.get("contentId", ""))
    port = str(params.get("port", "")) # port often corresponds to roomId
    status = str(params.get("status", "Ready")) # Content server reports its status

    if not port or not contentId:
         return {"status": "error", "message": "Missing port (roomId) or contentId for content_server_hello."}

    room_updated = False
    try:
        for room in rooms:
            if room["roomId"] == port:
                logging.info(f"Content server registered/updated for room {port}. Status: {status}")

                # Optional: Validate contentId if needed
                # if room["contentId"] != contentId:
                #     logging.error(f"Content ID mismatch for room {port}: Expected {room['contentId']}, got {contentId}")
                #     return {"status": "error", "message": "Content ID mismatch"}

                # Update room status based on content server report
                room["status"] = status
                room_updated = True

                # Respond with necessary info, like user pairings for this room
                return {
                    "status": "success",
                    "message": "Content server acknowledged.",
                    "roomId": room["roomId"],
                    "users": room["users"] # Send current user list to content server
                }

        if not room_updated:
             logging.warning(f"Received content_server_hello for unknown room: {port}")
             return {"status": "error", "message": f"Room {port} not found."}

    except Exception as e:
        logging.error(f"Error processing content_server_hello for room {port}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error processing content_server_hello: {e}"
        }


async def content_status_update(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    # Command sent *by* the content server to update the central server's room status
    logging.info(f"Received content_status_update command: {params}")
    contentId = str(params.get("contentId", ""))
    port = str(params.get("port", "")) # port == roomId
    new_status = str(params.get("status", ""))

    if not port or not new_status:
         return {"status": "error", "message": "Missing port (roomId) or status for content_status_update."}

    room_updated = False
    try:
        for room in rooms:
            if room["roomId"] == port:
                logging.info(f"Updating status for room {port} to {new_status} based on content server report.")

                # Optional: Validate contentId if provided and important
                # if contentId and room["contentId"] != contentId:
                #     logging.error(f"Content ID mismatch during status update for room {port}: Expected {room['contentId']}, got {contentId}")
                #     return {"status": "error", "message": "Content ID mismatch"}

                room["status"] = new_status
                room_updated = True

                # Acknowledge the update
                return {"status": "success", "message": "Status update acknowledged."}

        if not room_updated:
             logging.warning(f"Received content_status_update for unknown room: {port}")
             return {"status": "error", "message": f"Room {port} not found."}

    except Exception as e:
        logging.error(f"Error processing content_status_update for room {port}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error processing content_status_update: {e}"
        }


async def get_user_pairings(server_thread: WebSocketServerThread, client_websocket: websockets.WebSocketServerProtocol, params: Dict[str, Any]) -> Dict[str, Any]:
    # Command likely sent *by* a content server asking for the current user list for its room
    logging.info(f"Received get_user_pairings command: {params}")
    contentId = str(params.get("contentId", "")) # Optional validation
    port = str(params.get("port", "")) # port == roomId

    if not port:
         return {"status": "error", "message": "Missing port (roomId) for get_user_pairings."}

    room_found = False
    try:
        for room in rooms:
            if room["roomId"] == port:
                logging.info(f"Providing user pairings for room {port} to content server.")

                # Optional: Validate contentId if provided
                # if contentId and room["contentId"] != contentId:
                #     logging.error(f"Content ID mismatch during get_user_pairings for room {port}: Expected {room['contentId']}, got {contentId}")
                #     return {"status": "error", "message": "Content ID mismatch"}

                room_found = True
                return {
                    "status": "success",
                    "roomId": room["roomId"], # Corrected key from 'roonmId'
                    "users": room["users"]
                }

        if not room_found:
             logging.warning(f"Received get_user_pairings for unknown room: {port}")
             return {"status": "error", "message": f"Room {port} not found."}

    except Exception as e:
        logging.error(f"Error processing get_user_pairings for room {port}: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Error processing get_user_pairings: {e}"
        }


# --- PyQt GUI Class ---
# (No changes needed inside the GUI class itself for this request)
class OpServerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # Set up logging
        self.log_handler = QTextEditLogger()
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        # Remove all handlers to avoid duplicate logs
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            
        # Add our custom handler
        root_logger.addHandler(self.log_handler)
        
        self.server_thread = None
        
        self.init_ui()
    
    def get_local_ip(self):
        """Get the local IP address"""
        try:
            # Create a socket to determine the outgoing IP address
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # Google's DNS
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def init_ui(self):
        """Initialize the UI components"""
        self.setWindowTitle('OpServer GUI')
        self.setGeometry(100, 100, 800, 600)
        
        # Main layout
        main_layout = QVBoxLayout()
        
        # Server settings group
        server_group = QGroupBox("Server Settings")
        server_layout = QHBoxLayout()
        
        # Host IP
        host_layout = QVBoxLayout()
        host_label = QLabel("Host IP:")
        self.host_combo = QComboBox()
        
        # Add options for IP address binding
        self.host_combo.addItem("0.0.0.0 (All interfaces)")
        self.host_combo.addItem("127.0.0.1 (Localhost)")
        
        # Add local IP if available
        local_ip = self.get_local_ip()
        if local_ip != "127.0.0.1":
            self.host_combo.addItem(f"{local_ip} (This computer)")
        
        host_layout.addWidget(host_label)
        host_layout.addWidget(self.host_combo)
        
        # Port
        port_layout = QVBoxLayout()
        port_label = QLabel("Port:")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8766)
        port_layout.addWidget(port_label)
        port_layout.addWidget(self.port_spin)
        
        # Start/Stop server button
        self.server_btn = QPushButton("Start Server")
        self.server_btn.clicked.connect(self.toggle_server)
        
        # Add to server layout
        server_layout.addLayout(host_layout)
        server_layout.addLayout(port_layout)
        server_layout.addWidget(self.server_btn)
        server_group.setLayout(server_layout)
        
        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Server not running")
        
        # Connected clients group
        client_group = QGroupBox("Connected Clients")
        client_layout = QVBoxLayout()
        self.client_list = QTextEdit()
        self.client_list.setReadOnly(True)
        client_layout.addWidget(self.client_list)
        client_group.setLayout(client_layout)
        
        # Log output
        log_group = QGroupBox("Log Output")
        log_layout = QVBoxLayout()
        log_layout.addWidget(self.log_handler.widget)
        log_group.setLayout(log_layout)
        
        # Clear log button
        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_log)
        
        # Add everything to main layout
        main_layout.addWidget(server_group)
        main_layout.addWidget(client_group, 1)  # 1 = stretch factor
        main_layout.addWidget(log_group, 2)     # 2 = stretch factor (larger)
        main_layout.addWidget(self.clear_log_btn)
        
        # Create central widget and set layout
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)
        
        # Log initial message
        logging.info("OpServer GUI started")
    
    def toggle_server(self):
        """Start or stop the server"""
        if self.server_thread and self.server_thread.is_running:
            self.stop_server()
        else:
            self.start_server()
    
    def start_server(self):
        """Start the WebSocket server"""
        # Get host from combo box (remove any descriptions in parentheses)
        host_text = self.host_combo.currentText()
        host = host_text.split(' ')[0]  # Take first part before any space
        
        # Special case for "All interfaces"
        if host == "0.0.0.0":
            display_ip = "All interfaces"
        else:
            display_ip = host
            
        port = self.port_spin.value()
        
        # Disable server settings
        self.host_combo.setEnabled(False)
        self.port_spin.setEnabled(False)
        self.server_btn.setText("Stop Server")
        
        # Update status
        self.status_bar.showMessage(f"Starting server on {display_ip}:{port}...")
        
        # Create and start WebSocket server thread
        self.server_thread = WebSocketServerThread(host, port)
        
        # op agent commands
        self.server_thread.register_command_handler("HEARTBEAT", heartbeat)
        self.server_thread.register_command_handler("CLIENT_STATUS_UPDATE", client_status_update)
        #self.server_thread.register_command_handler("STATUS_UPDATE", status_update)
        #self.server_thread.register_command_handler("USER_PAIRED", user_paired)
        #self.server_thread.register_command_handler("START_CLIENT", start_client)
        #self.server_thread.register_command_handler("RESTART_CONTENT_CLIENT", restart_content_client)
        #self.server_thread.register_command_handler("CLOSE_CONTENT_CLIENT", close_content_client)

        # frontend commands
        self.server_thread.register_command_handler("GET_SYSTEM_STATUS", get_system_status)
        self.server_thread.register_command_handler("CREATE_ROOM", create_room)
        self.server_thread.register_command_handler("PAIR_USER", pair_user)
        self.server_thread.register_command_handler("SET_CONTENT_LANGUAGE", set_content_language)
        self.server_thread.register_command_handler("START_CONTENT", start_content)
        self.server_thread.register_command_handler("CHANGE_CONTENT_STATUS", change_content_status)
        self.server_thread.register_command_handler("RESTART_CONTENT_CLIENT", restart_content_client)
        self.server_thread.register_command_handler("CLOSE_CONTENT", close_content)
        self.server_thread.register_command_handler("RELEASE_ROOM", release_room)
        #self.server_thread.register_command_handler("ROOM_STATUS_UPDATE", room_status_update)

        # content server commands
        self.server_thread.register_command_handler("CONTENT_SERVER_HELLO", content_server_hello)
        self.server_thread.register_command_handler("CONTENT_STATUS_UPDATE", content_status_update)
        #self.server_thread.register_command_handler("SET_CONTENT_STATUS", set_content_status)
        self.server_thread.register_command_handler("GET_USER_PAIRINGS", get_user_pairings)
        #self.server_thread.register_command_handler("USER_PAIRING_UPDATED", user_pairing_updated)
        #self.server_thread.register_command_handler("CONTENT_LANGUAGE_SETTINGS", content_language_settings)
        #self.server_thread.register_command_handler("CLOSE_CONTENT_SERVER", close_content_server)

        # Connect signals
        self.server_thread.server_status.connect(self.update_server_status)
        self.server_thread.client_connected.connect(self.add_client)
        self.server_thread.client_disconnected.connect(self.remove_client)
        
        # Start thread
        self.server_thread.start()
    
    def stop_server(self):
        """Stop the server"""
        if self.server_thread:
            self.server_thread.stop()
            self.server_thread = None
        
        # Enable server settings
        self.host_combo.setEnabled(True)
        self.port_spin.setEnabled(True)
        self.server_btn.setText("Start Server")
        
        # Update status
        self.status_bar.showMessage("Server stopped")
        logging.info("Server stopped")
        
        # Clear client list
        connected_clients = []
        self.client_list.clear()
    
    @pyqtSlot(bool, str)
    def update_server_status(self, running, message):
        """Update server status in GUI"""
        if running:
            self.status_bar.showMessage(f"Server {message}")
        else:
            self.status_bar.showMessage(f"Server error: {message}")
            # Re-enable server settings
            self.host_combo.setEnabled(True)
            self.port_spin.setEnabled(True)
            self.server_btn.setText("Start Server")
    
    @pyqtSlot(str)
    def add_client(self, client_info):
        """Add client to the list"""
        connected_clients.append(client_info)
        self.update_client_list()
    
    @pyqtSlot(str)
    def remove_client(self, client_info):
        """Remove client from the list"""
        if client_info in connected_clients:
            connected_clients.remove(client_info)
        self.update_client_list()
    
    def update_client_list(self):
        """Update the client list display"""
        self.client_list.clear()
        if not connected_clients:
            self.client_list.setPlainText("No clients connected")
        else:
            for i, client in enumerate(connected_clients, 1):
                self.client_list.append(f"{i}. {client}")
    
    def clear_log(self):
        """Clear the log widget"""
        self.log_handler.widget.clear()
    
    def closeEvent(self, event):
        """Handle close event - stop server gracefully"""
        if self.server_thread and self.server_thread.is_running:
            self.stop_server()
            # Give some time for server to stop
            QTimer.singleShot(500, QApplication.instance().quit)
            event.ignore()
        else:
            event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(dark_stylesheet)

    window = OpServerGUI()
    window.show()
    sys.exit(app.exec_())
