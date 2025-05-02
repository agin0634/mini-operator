import asyncio
import json
import logging
import websockets
import sys
import time
import os
import socket
from datetime import datetime, timezone, timedelta
from typing import Dict, Callable, Any, Optional
from dark_theme import dark_stylesheet
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                           QHBoxLayout, QLabel, QLineEdit, QPushButton,
                           QTextEdit, QSpinBox, QGroupBox, QStatusBar)
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QThread, QTimer
from PyQt5.QtGui import QFont, QTextCursor

# 全域變數
config_settings = {
    "agent": {
        "vrId": "VR001"
    },
    "server": {
        "server_host": "127.0.0.1",
        "server_port": 8766
    }
}

# --- Configuration Loading ---
def load_config(file_name="client_settings.ini"):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    folder_path = os.path.join(script_dir, "configs")
    file_path = os.path.join(folder_path, file_name)
    global config_settings

    try:
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(config_settings, f, indent=4)
            return

        with open(file_path, "r", encoding="utf-8") as f:
            config_settings = json.load(f)
            return
    except FileNotFoundError:
        print(f"Configuration file {file_name} not found. Using default settings.")
        return

    except json.JSONDecodeError:
        print(f"Error decoding JSON from {file_name}. Using default settings.")
        return

# --- Logging Setup ---
class QTextEditLogger(logging.Handler, QObject):
    log_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        logging.Handler.__init__(self)
        QObject.__init__(self, parent)
        self.widget = QTextEdit(parent)
        self.widget.setReadOnly(True)
        self.widget.setFont(QFont("Courier", 10))
        self.log_signal.connect(self.append_log)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.setFormatter(formatter)

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg)

    @pyqtSlot(str)
    def append_log(self, message):
        self.widget.append(message)
        cursor = self.widget.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.widget.setTextCursor(cursor)

# --- Command Registry ---
class CommandRegistry:
    def __init__(self):
        self.handlers: Dict[str, Callable] = {}

    def register_handler(self, command_name: str, handler: Callable) -> None:
        self.handlers[command_name] = handler
        logging.info(f"Registered handler for command: {command_name}")

    def get_handler(self, command_name: str) -> Optional[Callable]:
        return self.handlers.get(command_name)

# --- WebSocket Client Thread ---
class WebSocketClientThread(QThread):
    connection_status = pyqtSignal(bool, str)
    message_received = pyqtSignal(dict) # Signal to pass received data to GUI if needed

    def __init__(self, uri, client_id):
        super().__init__()
        self.uri = uri
        self.client_id = client_id
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.is_running = False
        self.registry = CommandRegistry()
        self.reconnect_delay = 5 # seconds

    def register_command_handler(self, command_name: str, handler: Callable) -> None:
        self.registry.register_handler(command_name, handler)

    async def connect_to_server(self):
        while self.is_running:
            try:
                logging.info(f"Attempting to connect to {self.uri}...")
                self.websocket = await websockets.connect(self.uri)
                logging.info(f"Connected to {self.uri}")
                self.connection_status.emit(True, f"Connected to {self.uri}")

                # Send identity
                identity_msg = json.dumps({"type": "agent", "id": self.client_id})
                await self.websocket.send(identity_msg)
                logging.info(f"Sent identity: {identity_msg}")

                # Start listening for messages
                await self.receive_messages()

            except (websockets.exceptions.ConnectionClosedError, OSError) as e:
                logging.warning(f"Connection error: {e}. Reconnecting in {self.reconnect_delay}s...")
                self.connection_status.emit(False, f"Disconnected. Retrying...")
                if self.websocket:
                    await self.websocket.close()
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)
            except Exception as e:
                logging.error(f"Unexpected error in connect_to_server: {e}", exc_info=True)
                self.connection_status.emit(False, f"Error: {e}. Retrying...")
                if self.websocket:
                    await self.websocket.close()
                self.websocket = None
                await asyncio.sleep(self.reconnect_delay)

    async def receive_messages(self):
        if not self.websocket:
            return
        try:
            async for message_json in self.websocket:
                try:
                    data = json.loads(message_json)
                    logging.info(f"Received message: {data}")
                    self.message_received.emit(data) # Emit raw data if needed
                    await self.process_command(data)
                except json.JSONDecodeError:
                    logging.error(f"Invalid JSON received: {message_json}")
                except Exception as e:
                    logging.error(f"Error processing received message: {e}", exc_info=True)
        except websockets.exceptions.ConnectionClosed:
            logging.warning("Connection closed by server.")
            # The outer loop will handle reconnection
            raise websockets.exceptions.ConnectionClosedError(None, None) # Trigger reconnect

    async def process_command(self, data: Dict[str, Any]) -> None:
        command_name = data.get("command")
        if not command_name:
            # Might be a response to a previous request, not a command from server
            logging.debug(f"Received message without 'command' field: {data}")
            # Handle responses based on status or other fields if necessary
            return

        handler = self.registry.get_handler(command_name)
        if not handler:
            logging.warning(f"No handler found for command: {command_name}")
            return

        try:
            # Pass the client thread instance and the data to the handler
            await handler(self, data)
        except Exception as e:
            logging.error(f"Error executing handler for command '{command_name}': {e}", exc_info=True)

    async def send_message(self, message: Dict[str, Any]):
        if self.websocket and self.websocket.open:
            try:
                json_message = json.dumps(message)
                await self.websocket.send(json_message)
                logging.info(f"Sent message: {json_message}")
            except websockets.exceptions.ConnectionClosed:
                logging.warning("Cannot send message, connection is closed.")
            except Exception as e:
                logging.error(f"Error sending message: {e}", exc_info=True)
        else:
            logging.warning("Cannot send message, not connected.")

    async def run_async(self):
        self.is_running = True
        await self.connect_to_server() # This loop runs until stop() is called

    def run(self):
        asyncio.run(self.run_async())

    def stop(self):
        self.is_running = False
        # asyncio.create_task(self.websocket.close()) # Schedule close if running
        # Need a way to gracefully stop the asyncio loop from the Qt thread
        # For simplicity now, rely on the loop checking self.is_running

# --- Command Handlers (Agent Side) ---
# --- Command Handlers (Agent Side) ---
async def handle_start_client(client_thread: WebSocketClientThread, data: Dict[str, Any]):
    """Handles the START_CLIENT command from the server."""
    logging.info(f"Received START_CLIENT command: {data}")
    # TODO: Implement logic to start the client application/process
    # Example: client_thread.send_message({"status": "success", "command": "START_CLIENT_ACK", "details": "Client started"})
    pass

async def handle_restart_content_client(client_thread: WebSocketClientThread, data: Dict[str, Any]):
    """Handles the RESTART_CONTENT_CLIENT command from the server."""
    logging.info(f"Received RESTART_CONTENT_CLIENT command: {data}")
    # TODO: Implement logic to restart the content client process
    # Example: client_thread.send_message({"status": "success", "command": "RESTART_CONTENT_CLIENT_ACK", "details": "Content client restarting"})
    pass

async def handle_close_content_client(client_thread: WebSocketClientThread, data: Dict[str, Any]):
    """Handles the CLOSE_CONTENT_CLIENT command from the server."""
    logging.info(f"Received CLOSE_CONTENT_CLIENT command: {data}")
    # TODO: Implement logic to close the content client process
    # Example: client_thread.send_message({"status": "success", "command": "CLOSE_CONTENT_CLIENT_ACK", "details": "Content client closing"})
    pass

# --- Main GUI Window ---
class OpAgentGUI(QMainWindow):
    def __init__(self):
        super().__init__()

        # Logging
        self.log_handler = QTextEditLogger()
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        root_logger.addHandler(self.log_handler)

        self.client_thread = None
        self.init_ui()
        self.start_client() # Auto-connect on start

    def init_ui(self):
        self.setWindowTitle('OpAgent GUI')
        self.setGeometry(200, 200, 600, 400)

        main_layout = QVBoxLayout()

        # Connection Settings (Read-only display)
        connection_group = QGroupBox("Connection Settings")
        connection_layout = QHBoxLayout()
        self.server_uri_label = QLabel("Server URI: ws://127.0.0.1:8766") # Example URI
        self.client_id_label = QLabel("Client ID: agent_001") # Example ID
        connection_layout.addWidget(self.server_uri_label)
        connection_layout.addWidget(self.client_id_label)
        connection_group.setLayout(connection_layout)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Disconnected")

        # Log Output
        log_group = QGroupBox("Log Output")
        log_layout = QVBoxLayout()
        log_layout.addWidget(self.log_handler.widget)
        log_group.setLayout(log_layout)

        # Clear Log Button
        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_log)

        # Add widgets to main layout
        main_layout.addWidget(connection_group)
        main_layout.addWidget(log_group, 1) # Stretch factor
        main_layout.addWidget(self.clear_log_btn)

        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        logging.info("OpAgent GUI started")

    def start_client(self):
        # --- Configuration ---
        server_host = config_settings["server"]["server_host"]
        server_port = config_settings["server"]["server_port"]
        client_id = config_settings["agent"]["vrId"]
        # ---------------------

        uri = f"ws://{server_host}:{server_port}"
        self.server_uri_label.setText(f"Server URI: {uri}")
        self.client_id_label.setText(f"Client ID: {client_id}")

        if self.client_thread and self.client_thread.isRunning():
            logging.warning("Client thread already running.")
            return

        self.status_bar.showMessage(f"Connecting to {uri}...")
        self.client_thread = WebSocketClientThread(uri, client_id)

        # Register command handlers
        self.client_thread.register_command_handler("START_CLIENT", handle_start_client)
        self.client_thread.register_command_handler("RESTART_CONTENT_CLIENT", handle_restart_content_client)
        self.client_thread.register_command_handler("CLOSE_CONTENT_CLIENT", handle_close_content_client)
        # Add more handlers as needed based on server commands

        # Connect signals
        self.client_thread.connection_status.connect(self.update_connection_status)
        # self.client_thread.message_received.connect(self.handle_raw_message) # Optional

        self.client_thread.start()

    @pyqtSlot(bool, str)
    def update_connection_status(self, connected, message):
        self.status_bar.showMessage(message)
        if connected:
            logging.info(f"Connection status updated: {message}")
        else:
            logging.warning(f"Connection status updated: {message}")

    # Optional: Slot to handle raw messages if needed by the GUI directly
    # @pyqtSlot(dict)
    # def handle_raw_message(self, data):
    #     logging.debug(f"GUI received raw message: {data}")
    #     # Update UI elements based on raw data if necessary

    def clear_log(self):
        self.log_handler.widget.clear()

    def closeEvent(self, event):
        logging.info("Closing OpAgent GUI...")
        if self.client_thread:
            self.client_thread.stop()
            # Wait briefly for the thread to potentially stop its loop
            # self.client_thread.wait(500) # Use with caution, might block GUI
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(dark_stylesheet) # Apply the dark theme
    load_config() # Load configuration settings

    window = OpAgentGUI()
    window.show()
    sys.exit(app.exec_())
