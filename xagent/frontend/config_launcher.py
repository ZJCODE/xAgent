#!/usr/bin/env python3
"""
xAgent Configuration UI Launcher

A Streamlit-based web interface for configuring and managing xAgent HTTP servers.
"""

import sys
import argparse
import subprocess
import socket
from pathlib import Path


def is_port_available(host, port):
    """Check if a port is available for binding."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
            return True
    except OSError:
        return False


def find_available_port(host, start_port, max_attempts=10):
    """Find an available port starting from start_port."""
    for i in range(max_attempts):
        port = start_port + i
        if is_port_available(host, port):
            return port
    raise RuntimeError(f"Could not find an available port after {max_attempts} attempts starting from {start_port}")


def main():
    """Main entry point for xagent command."""
    parser = argparse.ArgumentParser(description="xAgent Configuration UI")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8502, help="Port to bind to")
    parser.add_argument("--browser", action="store_true", help="Open browser automatically")
    
    args = parser.parse_args()
    
    try:
        # Find an available port starting from the requested port
        available_port = find_available_port(args.host, args.port)
        
        if available_port != args.port:
            print(f"Port {args.port} is already in use, using port {available_port} instead")
        
        # Get the config UI file path
        config_ui_path = Path(__file__).parent / "config_ui.py"
        
        # Build streamlit command
        cmd = [
            "streamlit", "run", str(config_ui_path),
            "--server.address", args.host,
            "--server.port", str(available_port),
            "--server.headless", "true" if not args.browser else "false"
        ]
        
        if not args.browser:
            cmd.extend(["--browser.gatherUsageStats", "false"])
        
        print(f"Starting xAgent Configuration UI on {args.host}:{available_port}")
        print(f"Access the interface at: http://{args.host}:{available_port}")
        
        # Run streamlit
        subprocess.run(cmd)
        
    except KeyboardInterrupt:
        print("\nShutting down xAgent Configuration UI...")
        sys.exit(0)
    except Exception as e:
        print(f"Failed to start configuration UI: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
