#!/usr/bin/env python3
"""
xAgent Configuration UI Launcher

A Streamlit-based web interface for configuring and managing xAgent HTTP servers.
"""

import sys
import argparse
import subprocess
from pathlib import Path


def main():
    """Main entry point for xagent-config command."""
    parser = argparse.ArgumentParser(description="xAgent Configuration UI")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8502, help="Port to bind to")
    parser.add_argument("--browser", action="store_true", help="Open browser automatically")
    
    args = parser.parse_args()
    
    try:
        # Get the config UI file path
        config_ui_path = Path(__file__).parent / "config_ui.py"
        
        # Build streamlit command
        cmd = [
            "streamlit", "run", str(config_ui_path),
            "--server.address", args.host,
            "--server.port", str(args.port),
            "--server.headless", "true" if not args.browser else "false"
        ]
        
        if not args.browser:
            cmd.extend(["--browser.gatherUsageStats", "false"])
        
        print(f"Starting xAgent Configuration UI on {args.host}:{args.port}")
        print(f"Access the interface at: http://{args.host}:{args.port}")
        
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
