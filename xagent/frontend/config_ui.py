import streamlit as st
import yaml
import os
import json
import subprocess
import time
import requests
from typing import Dict, Any, List, Optional
from pathlib import Path
import signal
import psutil
from datetime import datetime

class AgentConfigUI:
    """Streamlit UI for configuring and managing xAgent HTTP servers."""
    
    def __init__(self):
        self.config_dir = Path("config")
        self.config_dir.mkdir(exist_ok=True)
        self.toolkit_dir = Path("toolkit")
        self.running_servers = self._load_server_registry()
        
    def _load_server_registry(self) -> Dict[str, Dict]:
        """Load running server registry from file."""
        registry_file = self.config_dir / "server_registry.json"
        if registry_file.exists():
            try:
                with open(registry_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def _save_server_registry(self):
        """Save running server registry to file."""
        registry_file = self.config_dir / "server_registry.json"
        with open(registry_file, 'w') as f:
            json.dump(self.running_servers, f, indent=2)
    
    def _check_server_health(self, url: str) -> bool:
        """Check if server is healthy."""
        try:
            # Handle different host formats
            if url.startswith("http://0.0.0.0:"):
                # Replace 0.0.0.0 with localhost for health check
                url = url.replace("0.0.0.0", "localhost")
            
            response = requests.get(f"{url}/health", timeout=5)
            return response.status_code == 200
        except requests.exceptions.ConnectionError:
            # Server is not ready yet
            return False
        except requests.exceptions.Timeout:
            # Server is taking too long to respond
            return False
        except Exception:
            return False
    
    def _check_prerequisites(self) -> bool:
        """Check if all prerequisites for starting a server are met."""
        # Check if OpenAI API key is set
        if not os.getenv('OPENAI_API_KEY'):
            st.error("❌ OPENAI_API_KEY environment variable is not set")
            st.info("Please set your OpenAI API key:")
            st.code("export OPENAI_API_KEY=your_api_key_here")
            return False
        
        # Check if xagent-server command is available
        try:
            result = subprocess.run(['xagent-server', '--help'], 
                                  capture_output=True, timeout=5)
            if result.returncode != 0:
                st.error("❌ xagent-server command failed")
                return False
        except FileNotFoundError:
            st.error("❌ xagent-server command not found")
            st.info("Please install xAgent: `pip install -e .`")
            return False
        except subprocess.TimeoutExpired:
            st.warning("⚠️ xagent-server command is slow to respond")
        except Exception as e:
            st.error(f"❌ Error checking xagent-server: {e}")
            return False
        
        return True
    
    def _cleanup_dead_servers(self):
        """Remove dead servers from registry."""
        dead_servers = []
        for server_id, server_info in self.running_servers.items():
            if not self._check_server_health(server_info['url']):
                # Check if process is still running
                try:
                    pid = server_info.get('pid')
                    if pid and psutil.pid_exists(pid):
                        continue
                except:
                    pass
                dead_servers.append(server_id)
        
        for server_id in dead_servers:
            del self.running_servers[server_id]
        
        if dead_servers:
            self._save_server_registry()
    
    def render_main_page(self):
        """Render the main configuration page."""
        st.set_page_config(
            page_title="xAgent Config Manager",
            page_icon="🤖",
            layout="wide"
        )
        
        st.title("🤖 xAgent Configuration Manager")
        st.markdown("Create, configure and manage xAgent HTTP servers through a visual interface.")
        
        # Sidebar for navigation
        with st.sidebar:
            st.header("Navigation")
            page = st.radio(
                "Choose a page:",
                ["Agent Configuration", "Server Management", "Running Servers"]
            )
        
        if page == "Agent Configuration":
            self.render_config_page()
        elif page == "Server Management":
            self.render_server_management()
        elif page == "Running Servers":
            self.render_running_servers()
    
    def render_config_page(self):
        """Render the agent configuration page."""
        st.header("🛠️ Agent Configuration")
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            # Basic Agent Configuration
            st.subheader("Basic Settings")
            
            agent_name = st.text_input(
                "Agent Name",
                value="MyAgent",
                help="Unique identifier for your agent"
            )
            
            system_prompt = st.text_area(
                "System Prompt",
                value="You are a helpful AI assistant.",
                height=100,
                help="Instructions that define the agent's behavior and personality"
            )
            
            model = st.selectbox(
                "Model",
                ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
                index=1,
                help="OpenAI model to use for the agent"
            )
            
            # Server Configuration
            st.subheader("Server Settings")
            
            col_host, col_port = st.columns(2)
            with col_host:
                host = st.text_input("Host", value="0.0.0.0")
            with col_port:
                port = st.number_input("Port", min_value=1000, max_value=65535, value=8010)
            
            # Tool Configuration
            st.subheader("Tools & Capabilities")
            
            # Built-in tools
            st.write("**Built-in Tools:**")
            enable_web_search = st.checkbox("Web Search", value=True)
            enable_draw_image = st.checkbox("Image Generation", value=False)
            
            # Custom tools
            st.write("**Custom Tools:**")
            custom_tools = st.text_area(
                "Custom Tool Names (one per line)",
                help="Enter the names of custom tools from your toolkit",
                placeholder="calculate_square\nfetch_weather"
            )
            
            # MCP Servers
            st.write("**MCP Servers:**")
            mcp_servers = st.text_area(
                "MCP Server URLs (one per line)",
                help="Enter URLs of MCP servers for dynamic tool loading",
                placeholder="http://localhost:8001/mcp/\nhttp://localhost:8002/mcp/"
            )
            
            # Advanced Settings
            with st.expander("Advanced Settings"):
                use_local_session = st.checkbox("Use Local Session", value=True, 
                                               help="If unchecked, will use Redis for session persistence")
                
                toolkit_path = st.text_input(
                    "Toolkit Path",
                    value="toolkit",
                    help="Path to custom toolkit directory"
                )
                
                # Sub-agents configuration
                st.write("**Sub-agents:**")
                sub_agents_enabled = st.checkbox("Enable Sub-agents")
                
                sub_agents_config = []
                if sub_agents_enabled:
                    num_sub_agents = st.number_input("Number of Sub-agents", min_value=1, max_value=10, value=1)
                    
                    for i in range(num_sub_agents):
                        with st.container():
                            st.write(f"Sub-agent {i+1}:")
                            col_name, col_desc = st.columns(2)
                            with col_name:
                                sub_name = st.text_input(f"Name", key=f"sub_name_{i}")
                            with col_desc:
                                sub_desc = st.text_input(f"Description", key=f"sub_desc_{i}")
                            sub_url = st.text_input(f"Server URL", key=f"sub_url_{i}")
                            
                            if sub_name and sub_desc and sub_url:
                                sub_agents_config.append({
                                    "name": sub_name,
                                    "description": sub_desc,
                                    "server_url": sub_url
                                })
            
            # Structured Output Configuration
            with st.expander("Structured Output (Optional)"):
                enable_structured_output = st.checkbox("Enable Structured Output")
                
                if enable_structured_output:
                    class_name = st.text_input("Class Name", value="ResponseModel")
                    
                    st.write("**Fields:**")
                    num_fields = st.number_input("Number of Fields", min_value=1, max_value=20, value=1)
                    
                    output_fields = {}
                    for i in range(num_fields):
                        col_field_name, col_field_type, col_field_desc = st.columns([1, 1, 2])
                        with col_field_name:
                            field_name = st.text_input(f"Field {i+1} Name", key=f"field_name_{i}")
                        with col_field_type:
                            field_type = st.selectbox(
                                f"Type",
                                ["str", "int", "float", "bool", "list", "dict"],
                                key=f"field_type_{i}"
                            )
                        with col_field_desc:
                            field_desc = st.text_input(f"Description", key=f"field_desc_{i}")
                        
                        if field_name:
                            output_fields[field_name] = {
                                "type": field_type,
                                "description": field_desc
                            }
        
        with col2:
            st.subheader("📋 Configuration Preview")
            
            # Build configuration
            config = self._build_config(
                agent_name, system_prompt, model, host, port,
                enable_web_search, enable_draw_image, custom_tools,
                mcp_servers, use_local_session, toolkit_path,
                sub_agents_config, enable_structured_output,
                output_fields if 'output_fields' in locals() else {},
                class_name if 'class_name' in locals() else ""
            )
            
            # Display YAML preview
            st.code(yaml.dump(config, default_flow_style=False), language="yaml")
            
            # Save and start buttons
            st.subheader("Actions")
            
            config_filename = st.text_input(
                "Config Filename",
                value=f"{agent_name.lower().replace(' ', '_')}_config.yaml"
            )
            
            col_save, col_start = st.columns(2)
            
            with col_save:
                if st.button("💾 Save Config", use_container_width=True):
                    config_path = self.config_dir / config_filename
                    with open(config_path, 'w') as f:
                        yaml.dump(config, f, default_flow_style=False)
                    st.success(f"Config saved to {config_path}")
                    time.sleep(1)
                    st.rerun()
            
            with col_start:
                if st.button("🚀 Start Server", use_container_width=True):
                    self._start_server(config, config_filename, toolkit_path)
                
                # Add debug option
                if st.checkbox("🐛 Debug Mode", help="Show detailed server logs during startup"):
                    st.session_state.debug_mode = True
                else:
                    st.session_state.debug_mode = False
    
    def _build_config(self, agent_name, system_prompt, model, host, port,
                     enable_web_search, enable_draw_image, custom_tools,
                     mcp_servers, use_local_session, toolkit_path,
                     sub_agents_config, enable_structured_output,
                     output_fields, class_name):
        """Build configuration dictionary."""
        config = {
            "agent": {
                "name": agent_name,
                "system_prompt": system_prompt,
                "model": model,
                "use_local_session": use_local_session
            },
            "server": {
                "host": host,
                "port": port
            }
        }
        
        # Add capabilities
        capabilities = {}
        
        # Tools
        tools = []
        if enable_web_search:
            tools.append("web_search")
        if enable_draw_image:
            tools.append("draw_image")
        
        # Custom tools
        if custom_tools.strip():
            custom_tool_list = [tool.strip() for tool in custom_tools.split('\n') if tool.strip()]
            tools.extend(custom_tool_list)
        
        if tools:
            capabilities["tools"] = tools
        
        # MCP servers
        if mcp_servers.strip():
            mcp_server_list = [server.strip() for server in mcp_servers.split('\n') if server.strip()]
            if mcp_server_list:
                capabilities["mcp_servers"] = mcp_server_list
        
        if capabilities:
            config["agent"]["capabilities"] = capabilities
        
        # Sub-agents
        if sub_agents_config:
            config["agent"]["sub_agents"] = sub_agents_config
        
        # Structured output
        if enable_structured_output and output_fields and class_name:
            config["agent"]["output_schema"] = {
                "class_name": class_name,
                "fields": output_fields
            }
        
        return config
    
    def _start_server(self, config, config_filename, toolkit_path):
        """Start the agent server."""
        debug_mode = getattr(st.session_state, 'debug_mode', False)
        
        try:
            # Check prerequisites
            if not self._check_prerequisites():
                return
                
            # Save config file
            config_path = self.config_dir / config_filename
            with open(config_path, 'w') as f:
                yaml.dump(config, f, default_flow_style=False)
            
            if debug_mode:
                st.info(f"📁 Config saved to: {config_path}")
            
            # Build command
            cmd = ["xagent-server", "--config", str(config_path)]
            if toolkit_path and toolkit_path.strip() and toolkit_path != "toolkit":
                cmd.extend(["--toolkit_path", toolkit_path])
            
            st.info(f"🚀 Starting server with command: {' '.join(cmd)}")
            
            # Start server process
            with st.spinner("Starting server..."):
                if debug_mode:
                    # In debug mode, show output in real-time
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                        preexec_fn=os.setsid,
                        cwd=os.getcwd(),
                        env=os.environ.copy()
                    )
                    
                    # Create containers for real-time output
                    output_container = st.empty()
                    output_lines = []
                    
                else:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        preexec_fn=os.setsid,
                        cwd=os.getcwd(),
                        env=os.environ.copy()
                    )
                
                # Wait and monitor server startup
                server_url = f"http://{config['server']['host']}:{config['server']['port']}"
                max_wait_time = 30  # Maximum wait time in seconds
                check_interval = 2  # Check every 2 seconds
                waited_time = 0
                
                while waited_time < max_wait_time:
                    time.sleep(check_interval)
                    waited_time += check_interval
                    
                    # In debug mode, read and display output
                    if debug_mode:
                        try:
                            # Read available output
                            while True:
                                line = process.stdout.readline()
                                if not line:
                                    break
                                output_lines.append(line.strip())
                                # Keep only last 20 lines
                                if len(output_lines) > 20:
                                    output_lines.pop(0)
                                
                            # Display output
                            if output_lines:
                                output_container.code('\n'.join(output_lines), language='text')
                        except:
                            pass
                    
                    # Check if process is still alive
                    if process.poll() is not None:
                        # Process has terminated
                        if debug_mode:
                            # Read remaining output
                            remaining_output = process.stdout.read()
                            if remaining_output:
                                output_lines.extend(remaining_output.strip().split('\n'))
                                output_container.code('\n'.join(output_lines), language='text')
                        else:
                            stdout, stderr = process.communicate()
                            stdout_str = stdout.decode() if stdout else ""
                            stderr_str = stderr.decode() if stderr else ""
                            
                            if stderr_str:
                                st.error(f"**Error output:**\n```\n{stderr_str}\n```")
                            if stdout_str:
                                st.info(f"**Standard output:**\n```\n{stdout_str}\n```")
                        
                        st.error("❌ Server process terminated unexpectedly")
                        return
                    
                    # Check if server is responding
                    if self._check_server_health(server_url):
                        # Server is up and running
                        server_id = f"{config['agent']['name']}_{config['server']['port']}"
                        self.running_servers[server_id] = {
                            "name": config['agent']['name'],
                            "url": server_url,
                            "config_file": config_filename,
                            "pid": process.pid,
                            "started_at": datetime.now().isoformat(),
                            "toolkit_path": toolkit_path
                        }
                        self._save_server_registry()
                        
                        st.success(f"✅ Server started successfully!")
                        st.info(f"🌐 Server URL: {server_url}")
                        st.info(f"🔗 Health Check: {server_url}/health")
                        st.info(f"⏱️ Startup time: {waited_time} seconds")
                        
                        if debug_mode and output_lines:
                            st.success("📊 Final startup logs:")
                            st.code('\n'.join(output_lines), language='text')
                        
                        return
                    
                    # Show progress
                    progress_msg = f"⏳ Waiting for server... ({waited_time}/{max_wait_time}s)"
                    if not debug_mode:
                        st.info(progress_msg)
                
                # Timeout reached
                st.error("❌ Server startup timeout")
                
                # Try to get any output before terminating
                if debug_mode:
                    st.warning("Server failed to start within timeout period")
                    if output_lines:
                        st.error("**Final output:**")
                        st.code('\n'.join(output_lines), language='text')
                else:
                    try:
                        stdout, stderr = process.communicate(timeout=3)
                        stdout_str = stdout.decode() if stdout else ""
                        stderr_str = stderr.decode() if stderr else ""
                        
                        if stderr_str:
                            st.error(f"**Error output:**\n```\n{stderr_str}\n```")
                        if stdout_str:
                            st.info(f"**Standard output:**\n```\n{stdout_str}\n```")
                            
                    except subprocess.TimeoutExpired:
                        st.warning("Could not retrieve process output")
                
                # Terminate the process
                try:
                    process.terminate()
                    time.sleep(2)
                    if process.poll() is None:
                        process.kill()
                except:
                    pass
        
        except FileNotFoundError:
            st.error("❌ `xagent-server` command not found. Please ensure xAgent is properly installed.")
            st.info("Try running: `pip install -e .` in the xAgent directory")
        except Exception as e:
            st.error(f"❌ Error starting server: {str(e)}")
            import traceback
            st.error(f"**Traceback:**\n```\n{traceback.format_exc()}\n```")
    
    def render_server_management(self):
        """Render server management page."""
        st.header("🔧 Server Management")
        
        # List saved configurations
        st.subheader("Saved Configurations")
        
        config_files = list(self.config_dir.glob("*.yaml"))
        config_files = [f for f in config_files if f.name != "server_registry.json"]
        
        if not config_files:
            st.info("No saved configurations found.")
            return
        
        for config_file in config_files:
            with st.expander(f"📄 {config_file.name}"):
                try:
                    with open(config_file, 'r') as f:
                        config = yaml.safe_load(f)
                    
                    col1, col2, col3 = st.columns([2, 1, 1])
                    
                    with col1:
                        st.write(f"**Agent:** {config.get('agent', {}).get('name', 'Unknown')}")
                        st.write(f"**Model:** {config.get('agent', {}).get('model', 'Unknown')}")
                        st.write(f"**Port:** {config.get('server', {}).get('port', 'Unknown')}")
                    
                    with col2:
                        if st.button(f"🚀 Start", key=f"start_{config_file.name}"):
                            self._start_server_from_file(config_file)
                    
                    with col3:
                        if st.button(f"🗑️ Delete", key=f"delete_{config_file.name}"):
                            os.remove(config_file)
                            st.success(f"Deleted {config_file.name}")
                            time.sleep(1)
                            st.rerun()
                    
                    # Show config content
                    with st.expander("View Configuration"):
                        st.code(yaml.dump(config, default_flow_style=False), language="yaml")
                
                except Exception as e:
                    st.error(f"Error reading config file: {str(e)}")
    
    def _start_server_from_file(self, config_file):
        """Start server from saved configuration file."""
        try:
            with open(config_file, 'r') as f:
                config = yaml.safe_load(f)
            
            toolkit_path = "toolkit"  # Default toolkit path
            self._start_server(config, config_file.name, toolkit_path)
            time.sleep(1)
            st.rerun()
        
        except Exception as e:
            st.error(f"Error starting server: {str(e)}")
    
    def render_running_servers(self):
        """Render running servers management page."""
        st.header("🚀 Running Servers")
        
        # Clean up dead servers first
        self._cleanup_dead_servers()
        
        if not self.running_servers:
            st.info("No running servers found.")
            return
        
        # Refresh button
        if st.button("🔄 Refresh", use_container_width=False):
            st.rerun()
        
        st.divider()
        
        for server_id, server_info in self.running_servers.items():
            with st.container():
                # Check server health
                is_healthy = self._check_server_health(server_info['url'])
                status_icon = "🟢" if is_healthy else "🔴"
                status_text = "Healthy" if is_healthy else "Unhealthy"
                
                col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
                
                with col1:
                    st.write(f"{status_icon} **{server_info['name']}**")
                    st.write(f"URL: {server_info['url']}")
                    st.write(f"Started: {server_info.get('started_at', 'Unknown')}")
                
                with col2:
                    st.write(f"**Status:** {status_text}")
                    st.write(f"**PID:** {server_info.get('pid', 'Unknown')}")
                
                with col3:
                    if st.button("🌐 Open", key=f"open_{server_id}"):
                        st.markdown(f"[Open Server]({server_info['url']}/health)")
                
                with col4:
                    if st.button("🛑 Stop", key=f"stop_{server_id}"):
                        self._stop_server(server_id, server_info)
                        time.sleep(1)
                        st.rerun()
                
                # Server details
                with st.expander(f"Details - {server_info['name']}"):
                    st.json(server_info)
                
                st.divider()
    
    def _stop_server(self, server_id, server_info):
        """Stop a running server."""
        try:
            pid = server_info.get('pid')
            if pid:
                try:
                    # Try graceful termination first
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(2)
                    
                    # Force kill if still running
                    if psutil.pid_exists(pid):
                        os.kill(pid, signal.SIGKILL)
                    
                    st.success(f"✅ Server {server_info['name']} stopped successfully")
                
                except ProcessLookupError:
                    st.info(f"Server {server_info['name']} was already stopped")
                except Exception as e:
                    st.error(f"Error stopping server: {str(e)}")
            
            # Remove from registry
            del self.running_servers[server_id]
            self._save_server_registry()
        
        except Exception as e:
            st.error(f"Failed to stop server: {str(e)}")


def main():
    """Main function to run the Streamlit app."""
    config_ui = AgentConfigUI()
    config_ui.render_main_page()


if __name__ == "__main__":
    main()
