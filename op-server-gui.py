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
            # Pass the entire data dictionary as parameters
            response_data = await handler(data) 
            
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

class Agent:
    def __init__(self, vr_id: str):
        self.vr_id = vr_id

    def __repr__(self):
        return f"Agent(agent_id={self.agent_id})"

# --- Command Handlers ---

async def ping_handler(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the 'ping' command."""
    logging.info(f"Received ping command with params: {params}")

    for c in connected_clients:
        try:
            #await c.send(json.dumps({"command": "start_client_handler", "message": "Ping received"}))
            logging.info("Hello")
        except Exception as e:
            logging.error(f"Error sending ping response: {e}")

    return {
        "status": "success",
        "message": "Pong!",
        "data": {"received_params": params} 
    }

async def calculate_sum(params: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the 'calculate_sum' command."""
    logging.info(f"Received calculate_sum command with params: {params}")
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
        logging.error(f"Invalid parameters for calculate_sum: {e}")
        return {
           "status": "error",
           "message": f"Invalid parameters: {e}. Please provide numbers for 'num1' and 'num2'.",
           "data": {}
        }

async def heartbeat(params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received heartbeat command with params: {params}")
    vrId = params.get("vrId", 0)
    timestamp = float(params.get("timestamp", 0))

    #taipei_tz = timezone(timedelta(hours=8))
    dt = datetime.fromtimestamp(timestamp)

    # 格式化為 ISO 8601 字串 (with timezone offset)
    server_time = dt.isoformat(timespec='seconds')
    try:
        for agent in agents:
            if agent["vrId"] == vrId:
                agent["connectionStatus"] = "Connected"
                return {
                    "status": "success",
                    "message": "Reconnect successful",
                    "serverTime": server_time
                }
            
        new_agent = {
            "vrId": vrId,
            "connectionStatus": "Connected",
            "currentContent": "無",
            "assignedRoom": "",
            "clientState": "idle"
        }

        agents.append(new_agent)
        logging.info(f"Agent {vrId} connected. Current agents: {agents}")

        return {
            "status": "success",
            "message": "Connect successful.",
            "serverTime": server_time
        }
    except (ValueError, TypeError) as e:
        logging.error(f"Invalid VrID")
        return {
           "status": "error",
           "message": "Add agent fail",
           "serverTime": server_time
        }

async def client_status_update(params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received client_status_update command with params: {params}")
    return {
        "status": "success",
        "message": "client_status_update!",
        "data": {"received_params": params}
    }

# --- OP FRONTEND COMMANDS ---
async def get_system_status(params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received get_system_status command with params: {params}")

    result = {
    "status": "success",
    "agents": agents,
    "rooms": rooms
    }   
    print(result)

    return result

async def create_room(params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received create_room command with params: {params}")
    vrIds = params.get("vrIds", [])
    langeuage = params.get("language")

    server_time = datetime.now(timezone.utc).isoformat(timespec='seconds')

    try:
        new_room = {
            "roomId": "8001",
            "status": "Idle",
            "contentName": "VR體驗A",
            "contentId": "Content001",
            "startTime": server_time,
            "assignedVRs": vrIds,
            "users": []
        }
        
        for vrId in vrIds:
            for agent in agents:
                if agent["vrId"] == vrId:
                    agent["assignedRoom"] = new_room["roomId"]
                    agent["clientState"] = "in_room"

                    new_user = {
                        "vrId": agent["vrId"],
                        "userToken": "",
                        "language": langeuage
                    }

                    new_room["users"].append(new_user)
                    break
        
        rooms.append(new_room)
        logging.info(f"Room created: {new_room}")

        return {
            "status": "success",
            "roomId": "8001",
            "assignedVRs": vrIds
        }
    except (ValueError, TypeError) as e:
        logging.error(f"Invalid VrID")
        return {
           "status": "error",
           "message": "create room fail",
           "serverTime": server_time
        }    

async def pair_user(params: Dict[str, Any]) -> Dict[str, Any]:
    logging.info(f"Received pair_user command with params: {params}")
    roomId = str(params.get("roomId", 0))
    vrId = params.get("vrId", 0)
    userToken = str(params.get("userToken", 0))

    try:
        for room in rooms:
            if room["roomId"] == roomId:
                for user in room["users"]:
                    if user["vrId"] == vrId:
                        user["userToken"] = userToken
                        logging.info(f"User paired: {user}")
                        return {
                            "status": "success",
                            "message": "User paired successfully",
                            "roomId": roomId,
                            "vrId": vrId,
                            "userToken": userToken
                        }
        
        logging.error(f"Room or VR ID not found")
        return {
            "status": "error",
            "message": "Room or VR ID not found",
            "data": {}
        }
    except (ValueError, TypeError) as e:
        logging.error(f"Invalid parameters for pair_user: {e}")
        return {
            "status": "error",
            "message": f"Invalid parameters: {e}. Please provide valid 'roomId', 'vrId', and 'userToken'.",
            "data": {}
        }
    
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
        
        # Register command handlers
        self.server_thread.register_command_handler("PING", ping_handler)
        self.server_thread.register_command_handler("CALCULATE_SUM", calculate_sum)

        # op agent commands
        self.server_thread.register_command_handler("HEARTBEAT", heartbeat)
        self.server_thread.register_command_handler("CLIENT_STATUS_UPDATE", client_status_update)

        # frontend commands
        self.server_thread.register_command_handler("GET_SYSTEM_STATUS", get_system_status)
        self.server_thread.register_command_handler("CREATE_ROOM", create_room)
        self.server_thread.register_command_handler("PAIR_USER", pair_user)
        #self.server_thread.register_command_handler("SET_CONTENT_LANGUAGE", set_content_language)
        #self.server_thread.register_command_handler("START_CONTENT", start_content)
        #self.server_thread.register_command_handler("CHANGE_CONTENT_STATUS", change_content_status)
        #self.server_thread.register_command_handler("RESTART_CONTENT_CLIENT", restart_content_client)
        #self.server_thread.register_command_handler("CLOSE_CONTENT", close_content)
        #self.server_thread.register_command_handler("RELEASE_ROOM", release_room)
        #self.server_thread.register_command_handler("ROOM_STATUS_UPDATE", room_status_update)

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