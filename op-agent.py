#!/usr/bin/env python3
# op-agent-gui.py
# PyQt GUI for op-agent with connection settings and logging window

import sys
import logging
import asyncio
import json
import uuid
import time
import websockets
import socket
from typing import Dict, Callable, Any, Optional
from datetime import datetime
from dark_theme import dark_stylesheet

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                           QTextEdit, QSpinBox, QGroupBox, QSplitter, 
                           QStatusBar, QComboBox, QMessageBox)
from PyQt5.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QThread, QTimer
from PyQt5.QtGui import QFont, QTextCursor

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

class WebSocketClientThread(QThread):
    # Signals
    connection_status = pyqtSignal(bool, str)
    message_received = pyqtSignal(str)
    
    def __init__(self, host, port, vr_id):
        super().__init__()
        self.host = host
        self.port = port
        self.vr_id = vr_id or str(uuid.uuid4())
        self.is_running = False
        self.is_connected = False
        self.websocket = None
        self.registry = CommandRegistry()
        self.heartbeat_timer = None
        self.reconnect_interval = 5  # seconds
    
    def register_command_handler(self, command_name: str, handler: Callable) -> None:
        """Register a handler function for a command"""
        self.registry.register_handler(command_name, handler)
    
    async def connect(self):
        """Connect to the WebSocket server"""
        uri = f"ws://{self.host}:{self.port}"
        try:
            self.websocket = await websockets.connect(uri)
            self.is_connected = True
            self.connection_status.emit(True, f"Connected to {uri}")
            logging.info(f"Connected to server at {uri}")
            
            # Send heartbeat immediately
            await self.send_heartbeat()
            
            # Start listening loop
            await self.listen()
            
        except Exception as e:
            self.is_connected = False
            error_msg = f"Failed to connect to {uri}: {str(e)}"
            self.connection_status.emit(False, error_msg)
            logging.error(error_msg)
            
            # Schedule reconnection
            await asyncio.sleep(self.reconnect_interval)
            if self.is_running:
                asyncio.create_task(self.connect())
    
    async def send_command(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send a command to the server and wait for response"""
        if not self.is_connected or not self.websocket:
            logging.error("Cannot send command: Not connected to server")
            return None
        
        try:
            message_json = json.dumps(data)
            await self.websocket.send(message_json)
            logging.debug(f"Sent command: {data.get('command')}")
            
            # Wait for response
            response_json = await self.websocket.recv()
            response = json.loads(response_json)
            return response
            
        except websockets.exceptions.ConnectionClosed:
            logging.warning("Connection closed while sending command")
            self.is_connected = False
            self.connection_status.emit(False, "Connection closed")
            return None
        except Exception as e:
            logging.error(f"Error sending command: {str(e)}")
            return None
    
    async def send_heartbeat(self):
        """Send heartbeat to server"""
        if not self.is_connected:
            return
            
        heartbeat_data = {
            "command": "HEARTBEAT",
            "vrId": self.vr_id,
            "timestamp": time.time()
        }
        
        try:
            response = await self.send_command(heartbeat_data)

            if response: #and response.get("status") == "success":
                logging.debug("Heartbeat successful")
            else:
                logging.warning(f"Heartbeat failed: {response}")
        except Exception as e:
            logging.error(f"Error sending heartbeat: {str(e)}")
    
    async def send_status_update(self, status: str):
        """Send status update to server"""
        status_data = {
            "command": "CLIENT_STATUS_UPDATE",
            "vrId": self.vr_id,
            "status": status,
            "timestamp": time.time()
        }
        
        try:
            await self.send_command(status_data)
            logging.info(f"Sent status update: {status}")
        except Exception as e:
            logging.error(f"Failed to send status update: {str(e)}")
    
    async def process_message(self, message_json: str):
        """Process a message from the server"""
        try:
            data = json.loads(message_json)
            self.message_received.emit(message_json)

            status = data.get("status")
            if status:
                logging.info(data)
                return

            command_name = data.get("command")
            if not command_name:
                logging.warning("Received message without command")
                return
                
            handler = self.registry.get_handler(command_name)
            if handler:
                try:
                    await handler(data, self)
                except Exception as e:
                    logging.error(f"Error in handler for {command_name}: {str(e)}")
            else:
                logging.info(f"No handler for command: {command_name}")
                
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON received: {message_json}")
        except Exception as e:
            logging.error(f"Error processing message: {str(e)}")
    
    async def listen(self):
        """Listen for messages from the server"""
        if not self.websocket:
            return
            
        try:
            async for message in self.websocket:
                if not self.is_running:
                    break
                await self.process_message(message)
                
        except websockets.exceptions.ConnectionClosed:
            logging.warning("Connection to server closed")
            self.is_connected = False
            self.connection_status.emit(False, "Connection closed")
            
            # Attempt to reconnect
            if self.is_running:
                await asyncio.sleep(self.reconnect_interval)
                asyncio.create_task(self.connect())
                
        except Exception as e:
            logging.error(f"Error in listen loop: {str(e)}")
            self.is_connected = False
            self.connection_status.emit(False, f"Error: {str(e)}")
            
            # Attempt to reconnect
            if self.is_running:
                await asyncio.sleep(self.reconnect_interval)
                asyncio.create_task(self.connect())
    
    async def heartbeat_loop(self):
        """Send periodic heartbeats"""
        while self.is_running:
            await asyncio.sleep(10)  # Send heartbeat every 30 seconds
            if self.is_connected:
                await self.send_heartbeat()
    
    async def run_async(self):
        """Main async entry point"""
        self.is_running = True
        
        # Start heartbeat task
        heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        
        # Initial connection
        connection_task = asyncio.create_task(self.connect())
        
        # Wait until stopped
        while self.is_running:
            await asyncio.sleep(0.1)
        
        # Clean up
        heartbeat_task.cancel()
        connection_task.cancel()
        
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
    
    async def stop_async(self):
        """Stop the client"""
        self.is_running = False
        if self.is_connected and self.websocket:
            try:
                await self.send_status_update("DISCONNECTING")
                await self.websocket.close()
            except:
                pass
        self.is_connected = False
        self.websocket = None
    
    def run(self):
        """QThread entry point"""
        asyncio.run(self.run_async())
    
    def stop(self):
        """Stop the thread"""
        self.is_running = False
        # We need to run stop_async in the event loop
        if hasattr(asyncio, "create_task"):
            asyncio.create_task(self.stop_async())

# --- Command Handlers ---
async def start_client_handler(data: Dict[str, Any], client):
    """Handle START_CLIENT command"""
    logging.info(f"Received START_CLIENT command: {data}")
    # Add implementation here
    
    # Send response
    await client.send_status_update("STARTED")

async def restart_content_client_handler(data: Dict[str, Any], client):
    """Handle RESTART_CONTENT_CLIENT command"""
    logging.info(f"Received RESTART_CONTENT_CLIENT command: {data}")
    # Add implementation here
    
    # Send response
    await client.send_command({
        "command": "STATUS_UPDATE",
        "vrId": client.vr_id,
        "status": "RESTARTING_CONTENT",
        "timestamp": time.time()
    })

async def close_content_client_handler(data: Dict[str, Any], client):
    """Handle CLOSE_CONTENT_CLIENT command"""
    logging.info(f"Received CLOSE_CONTENT_CLIENT command: {data}")
    # Add implementation here
    
    # Send response
    await client.send_command({
        "command": "STATUS_UPDATE",
        "vrId": client.vr_id,
        "status": "CONTENT_CLOSED",
        "timestamp": time.time()
    })

class OpAgentGUI(QMainWindow):
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
        
        self.client_thread = None
        self.vr_id = str(uuid.uuid4())
        
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
        self.setWindowTitle('OpAgent GUI')
        self.setGeometry(100, 100, 800, 600)
        
        # Main layout
        main_layout = QVBoxLayout()
        
        # Connection settings group
        conn_group = QGroupBox("Connection Settings")
        conn_layout = QHBoxLayout()
        
        # Host IP
        host_layout = QVBoxLayout()
        host_label = QLabel("Host IP:")
        self.host_combo = QComboBox()
        
        # Add localhost
        self.host_combo.addItem("127.0.0.1")
        
        # Add local IP if available
        local_ip = self.get_local_ip()
        if local_ip != "127.0.0.1":
            self.host_combo.addItem(local_ip)
            
        # Custom entry option
        self.host_combo.setEditable(True)
        
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
        
        # VRID
        vrid_layout = QVBoxLayout()
        vrid_label = QLabel("VR ID:")
        self.vrid_edit = QLineEdit(self.vr_id)
        vrid_layout.addWidget(vrid_label)
        vrid_layout.addWidget(self.vrid_edit)
        
        # Connect button
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        
        # Add to connection layout
        conn_layout.addLayout(host_layout)
        conn_layout.addLayout(port_layout)
        conn_layout.addLayout(vrid_layout)
        conn_layout.addWidget(self.connect_btn)
        conn_group.setLayout(conn_layout)
        
        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Not connected")
        
        # Log output
        log_group = QGroupBox("Log Output")
        log_layout = QVBoxLayout()
        log_layout.addWidget(self.log_handler.widget)
        log_group.setLayout(log_layout)
        
        # Clear log button
        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_log)
        
        # Add everything to main layout
        main_layout.addWidget(conn_group)
        main_layout.addWidget(log_group)
        main_layout.addWidget(self.clear_log_btn)
        
        # Create central widget and set layout
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)
        
        # Log initial message
        logging.info("OpAgent GUI started")
    
    def toggle_connection(self):
        """Connect or disconnect from server"""
        if self.client_thread and self.client_thread.is_running:
            self.disconnect_from_server()
        else:
            self.connect_to_server()
    
    def connect_to_server(self):
        """Connect to the WebSocket server"""
        host = self.host_combo.currentText().strip()
        port = self.port_spin.value()
        vr_id = self.vrid_edit.text().strip()
        
        if not host:
            QMessageBox.warning(self, "Input Error", "Please enter a valid host address.")
            return
            
        if not vr_id:
            vr_id = str(uuid.uuid4())
            self.vrid_edit.setText(vr_id)
        
        # Disable connection inputs
        self.host_combo.setEnabled(False)
        self.port_spin.setEnabled(False)
        self.vrid_edit.setEnabled(False)
        self.connect_btn.setText("Disconnect")
        
        # Update status
        self.status_bar.showMessage(f"Connecting to {host}:{port}...")
        
        # Create and start WebSocket client thread
        self.client_thread = WebSocketClientThread(host, port, vr_id)
        
        # Register handlers
        self.client_thread.register_command_handler("START_CLIENT", start_client_handler)
        self.client_thread.register_command_handler("RESTART_CONTENT_CLIENT", restart_content_client_handler)
        self.client_thread.register_command_handler("CLOSE_CONTENT_CLIENT", close_content_client_handler)
        
        # Connect signals
        self.client_thread.connection_status.connect(self.update_connection_status)
        self.client_thread.message_received.connect(self.on_message_received)
        
        # Start thread
        self.client_thread.start()
    
    def disconnect_from_server(self):
        """Disconnect from server"""
        if self.client_thread:
            self.client_thread.stop()
            self.client_thread = None
        
        # Enable connection inputs
        self.host_combo.setEnabled(True)
        self.port_spin.setEnabled(True)
        self.vrid_edit.setEnabled(True)
        self.connect_btn.setText("Connect")
        
        # Update status
        self.status_bar.showMessage("Disconnected")
        logging.info("Disconnected from server")
    
    @pyqtSlot(bool, str)
    def update_connection_status(self, connected, message):
        """Update connection status in GUI"""
        if connected:
            self.status_bar.showMessage(f"Connected: {message}")
        else:
            self.status_bar.showMessage(f"Disconnected: {message}")
            # Enable connection inputs
            self.host_combo.setEnabled(True)
            self.port_spin.setEnabled(True)
            self.vrid_edit.setEnabled(True)
            self.connect_btn.setText("Connect")
    
    @pyqtSlot(str)
    def on_message_received(self, message):
        """Handle received message"""
        # This is mostly for logging - actual message handling is done in the thread
        try:
            data = json.loads(message)
            command_name = data.get("command")
            if command_name:
                logging.debug(f"Received message: {command_name}")
                return
            else:
                logging.debug(data)
                return
            
        except:
            pass
    
    def clear_log(self):
        """Clear the log widget"""
        self.log_handler.widget.clear()
    
    def closeEvent(self, event):
        """Handle close event - disconnect gracefully"""
        if self.client_thread and self.client_thread.is_running:
            self.disconnect_from_server()
            # Give some time for disconnect to complete
            QTimer.singleShot(500, QApplication.instance().quit)
            event.ignore()
        else:
            event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(dark_stylesheet)

    window = OpAgentGUI()
    window.show()
    sys.exit(app.exec_())