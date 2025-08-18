#!/usr/bin/env python3
"""
Launcher script for xAgent Streamlit web interface.
This script properly launches the Streamlit app using streamlit run.
"""

import sys
import subprocess
import os
import argparse
import socket
from pathlib import Path


def is_port_available(host, port):
    """Check if a port is available on the given host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            result = sock.bind((host, port))
            return True
    except (OSError, socket.error):
        return False


def find_available_port(host, start_port, max_attempts=100):
    """Find an available port starting from start_port."""
    port = start_port
    for _ in range(max_attempts):
        if is_port_available(host, port):
            return port
        port += 1
    raise RuntimeError(f"Could not find an available port after {max_attempts} attempts starting from {start_port}")


def main():
    """Launch the Streamlit app using streamlit run command."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Launch xAgent Web UI")
    parser.add_argument(
        "--agent-server",
        default="http://localhost:8010",
        help="URL of the xAgent server (default: http://localhost:8010)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host address for Streamlit server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        default="8501",
        help="Port for Streamlit server (default: 8501)"
    )
    
    args = parser.parse_args()
    
    # Convert port to integer
    requested_port = int(args.port)
    
    # Find an available port starting from the requested port
    try:
        available_port = find_available_port(args.host, requested_port)
        if available_port != requested_port:
            print(f"⚠️  Port {requested_port} is occupied, using port {available_port} instead")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    # Get the directory where this launcher script is located
    script_dir = Path(__file__).parent
    app_path = script_dir / "web.py"
    
    # Ensure the web.py file exists
    if not app_path.exists():
        print(f"Error: Could not find web.py at {app_path}")
        sys.exit(1)
    
    # Set environment variables for the Streamlit app
    env = os.environ.copy()
    env["XAGENT_SERVER_URL"] = args.agent_server
    
    # Build the streamlit run command
    cmd = [
        sys.executable, "-m", "streamlit", "run", 
        str(app_path),
        "--server.headless", "true",
        "--server.port", str(available_port),
        "--server.address", args.host
    ]
    
    try:
        # Launch streamlit
        print("🚀 Starting xAgent Web UI...")
        print(f"📍 The web interface will be available at: http://{args.host}:{available_port}")
        print(f"🔗 Agent server URL: {args.agent_server}")
        print("🔧 Use Ctrl+C to stop the server")
        print()
        subprocess.run(cmd, check=True, env=env)
    except subprocess.CalledProcessError as e:
        print(f"Error launching Streamlit app: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down xAgent Web UI...")
        sys.exit(0)

if __name__ == "__main__":
    main()
