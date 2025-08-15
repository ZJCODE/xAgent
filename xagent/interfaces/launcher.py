#!/usr/bin/env python3
"""
Unified launcher for xAgent - starts both server and web interface.
"""

import os
import sys
import time
import signal
import argparse
import subprocess
import threading
from pathlib import Path
from typing import Optional, List


class XAgentLauncher:
    """Unified launcher for xAgent server and web interface."""
    
    def __init__(self):
        self.server_process: Optional[subprocess.Popen] = None
        self.web_process: Optional[subprocess.Popen] = None
        self.shutdown_event = threading.Event()
        
    def signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        print(f"\n🛑 Received signal {signum}, shutting down...")
        self.shutdown()
        sys.exit(0)
        
    def shutdown(self):
        """Shutdown both server and web processes."""
        print("🔄 Shutting down xAgent services...")
        
        # Set shutdown event
        self.shutdown_event.set()
        
        # Terminate web process first
        if self.web_process and self.web_process.poll() is None:
            print("🌐 Stopping web interface...")
            try:
                self.web_process.terminate()
                self.web_process.wait(timeout=5)
                print("✅ Web interface stopped")
            except subprocess.TimeoutExpired:
                print("⚠️  Force killing web interface...")
                self.web_process.kill()
                self.web_process.wait()
                
        # Terminate server process
        if self.server_process and self.server_process.poll() is None:
            print("🖥️  Stopping server...")
            try:
                self.server_process.terminate()
                self.server_process.wait(timeout=5)
                print("✅ Server stopped")
            except subprocess.TimeoutExpired:
                print("⚠️  Force killing server...")
                self.server_process.kill()
                self.server_process.wait()
                
        print("🎉 xAgent services stopped successfully")
        
    def start_server(self, config: Optional[str] = None, toolkit_path: Optional[str] = None, 
                    host: str = "localhost", port: int = 8010) -> bool:
        """Start the xAgent server."""
        cmd = [sys.executable, "-m", "xagent.interfaces.server"]
        
        if config:
            cmd.extend(["--config", config])
        if toolkit_path:
            cmd.extend(["--toolkit_path", toolkit_path])
        if host:
            cmd.extend(["--host", host])
        if port:
            cmd.extend(["--port", str(port)])
            
        try:
            print(f"🖥️  Starting xAgent server on {host}:{port}...")
            self.server_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # Wait a moment to check if server started successfully
            time.sleep(2)
            if self.server_process.poll() is not None:
                stdout, stderr = self.server_process.communicate()
                print(f"❌ Server failed to start:")
                if stdout:
                    print(f"STDOUT: {stdout}")
                if stderr:
                    print(f"STDERR: {stderr}")
                return False
                
            print(f"✅ Server started successfully on {host}:{port}")
            return True
            
        except Exception as e:
            print(f"❌ Failed to start server: {e}")
            return False
            
    def start_web(self, agent_server: str = "http://localhost:8010", 
                 host: str = "0.0.0.0", port: int = 8501) -> bool:
        """Start the web interface."""
        cmd = [sys.executable, "-m", "xagent.frontend.launcher"]
        cmd.extend(["--agent-server", agent_server])
        cmd.extend(["--host", host])
        cmd.extend(["--port", str(port)])
        
        try:
            print(f"🌐 Starting web interface on {host}:{port}...")
            self.web_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # Wait a moment to check if web started successfully
            time.sleep(3)
            if self.web_process.poll() is not None:
                stdout, stderr = self.web_process.communicate()
                print(f"❌ Web interface failed to start:")
                if stdout:
                    print(f"STDOUT: {stdout}")
                if stderr:
                    print(f"STDERR: {stderr}")
                return False
                
            print(f"✅ Web interface started successfully")
            print(f"🔗 Access the web interface at: http://{host}:{port}")
            return True
            
        except Exception as e:
            print(f"❌ Failed to start web interface: {e}")
            return False
            
    def monitor_processes(self):
        """Monitor both processes and restart if needed."""
        while not self.shutdown_event.is_set():
            try:
                # Check server process
                if self.server_process and self.server_process.poll() is not None:
                    print("⚠️  Server process died unexpectedly")
                    return False
                    
                # Check web process
                if self.web_process and self.web_process.poll() is not None:
                    print("⚠️  Web process died unexpectedly")
                    return False
                    
                # Wait a bit before next check
                time.sleep(1)
                
            except KeyboardInterrupt:
                break
                
        return True
        
    def run(self, config: Optional[str] = None, toolkit_path: Optional[str] = None,
           server_host: str = "localhost", server_port: int = 8010,
           web_host: str = "0.0.0.0", web_port: int = 8501,
           server_only: bool = False, web_only: bool = False):
        """Run the xAgent launcher."""
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        try:
            success = True
            
            if not web_only:
                # Start server
                if not self.start_server(config, toolkit_path, server_host, server_port):
                    return False
                    
            if not server_only:
                # Start web interface
                agent_server_url = f"http://{server_host}:{server_port}"
                if not self.start_web(agent_server_url, web_host, web_port):
                    if not web_only:
                        self.shutdown()
                    return False
                    
            print("\n🎉 xAgent is running!")
            if not web_only:
                print(f"🖥️  Server: http://{server_host}:{server_port}")
            if not server_only:
                print(f"🌐 Web UI: http://{web_host}:{web_port}")
            print("🔧 Press Ctrl+C to stop all services")
            print()
            
            # Monitor processes
            self.monitor_processes()
            
        except KeyboardInterrupt:
            print("\n🛑 Received interrupt signal")
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            success = False
        finally:
            self.shutdown()
            
        return success


def main():
    """Main entry point for xagent command."""
    parser = argparse.ArgumentParser(
        description="xAgent Unified Launcher - Start server and web interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  xagent                                    # Start both server and web with defaults
  xagent --server-only                     # Start only the server
  xagent --web-only                        # Start only the web interface
  xagent --config config/agent.yaml        # Use custom config
  xagent --server-port 8020 --web-port 8502  # Custom ports
        """
    )
    
    # General options
    parser.add_argument(
        "--config", 
        help="Path to agent configuration file"
    )
    parser.add_argument(
        "--toolkit-path", 
        help="Path to toolkit directory for additional tools"
    )
    
    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--server-only", 
        action="store_true",
        help="Start only the server (no web interface)"
    )
    mode_group.add_argument(
        "--web-only", 
        action="store_true",
        help="Start only the web interface (assumes server is running)"
    )
    
    # Server options
    server_group = parser.add_argument_group("Server Options")
    server_group.add_argument(
        "--server-host", 
        default="localhost",
        help="Host for the agent server (default: localhost)"
    )
    server_group.add_argument(
        "--server-port", 
        type=int, 
        default=8010,
        help="Port for the agent server (default: 8010)"
    )
    
    # Web options
    web_group = parser.add_argument_group("Web Interface Options")
    web_group.add_argument(
        "--web-host", 
        default="0.0.0.0",
        help="Host for the web interface (default: 0.0.0.0)"
    )
    web_group.add_argument(
        "--web-port", 
        type=int, 
        default=8501,
        help="Port for the web interface (default: 8501)"
    )
    
    args = parser.parse_args()
    
    # Create and run launcher
    launcher = XAgentLauncher()
    success = launcher.run(
        config=args.config,
        toolkit_path=args.toolkit_path,
        server_host=args.server_host,
        server_port=args.server_port,
        web_host=args.web_host,
        web_port=args.web_port,
        server_only=args.server_only,
        web_only=args.web_only
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
