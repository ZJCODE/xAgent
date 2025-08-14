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
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(exist_ok=True)
        self.toolkit_dir = Path("toolkit")
        self.running_servers = self._load_server_registry()
        self.running_webchats = self._load_webchat_registry()
        
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
    
    def _load_webchat_registry(self) -> Dict[str, Dict]:
        """Load running web chat registry from file."""
        registry_file = self.config_dir / "webchat_registry.json"
        if registry_file.exists():
            try:
                with open(registry_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def _save_webchat_registry(self):
        """Save running web chat registry to file."""
        registry_file = self.config_dir / "webchat_registry.json"
        with open(registry_file, 'w') as f:
            json.dump(self.running_webchats, f, indent=2)
    
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
    
    def _check_webchat_health(self, url: str) -> bool:
        """Check if web chat is healthy."""
        try:
            response = requests.get(url, timeout=5)
            return response.status_code == 200
        except:
            return False
    
    def _cleanup_dead_webchats(self):
        """Remove dead web chats from registry."""
        dead_webchats = []
        for webchat_id, webchat_info in self.running_webchats.items():
            if not self._check_webchat_health(webchat_info['url']):
                # Check if process is still running
                try:
                    pid = webchat_info.get('pid')
                    if pid and psutil.pid_exists(pid):
                        continue
                except:
                    pass
                dead_webchats.append(webchat_id)
        
        for webchat_id in dead_webchats:
            del self.running_webchats[webchat_id]
        
        if dead_webchats:
            self._save_webchat_registry()
    
    def _find_available_port(self, start_port: int = 8501) -> int:
        """Find an available port starting from start_port."""
        import socket
        port = start_port
        while port < start_port + 100:  # Try up to 100 ports
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('localhost', port))
                    return port
            except OSError:
                port += 1
        raise Exception(f"No available port found starting from {start_port}")
    
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
        
        # Check for existing config files
        config_files = list(self.config_dir.glob("*.yaml"))
        config_files = [f for f in config_files if f.name not in ["server_registry.json", "webchat_registry.json"]]
        
        # Load from existing config section
        col_mode, col_file = st.columns([1, 2])
        
        with col_mode:
            config_mode = st.radio(
                "Choose mode:",
                ["Create New", "Edit Existing"],
                help="Create a new configuration or edit an existing one",
                horizontal=True,
                label_visibility="visible"
            )
        
        # Initialize default values
        loaded_config = None
        if config_mode == "Edit Existing" and config_files:
            with col_file:
                selected_file = st.selectbox(
                    "Select config file:",
                    options=[f.name for f in config_files],
                    help="Choose an existing configuration file to edit"
                )
                
                if selected_file:
                    config_path = self.config_dir / selected_file
                    try:
                        with open(config_path, 'r', encoding='utf-8') as f:
                            loaded_config = yaml.safe_load(f)
                        # st.success(f"✅ Loaded configuration: {selected_file}")
                    except Exception as e:
                        st.error(f"❌ Error loading config: {str(e)}")
        elif config_mode == "Edit Existing" and not config_files:
            st.info("No existing configuration files found. Switch to 'Create New' mode.")
            config_mode = "Create New"
        
        st.divider()
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            # Basic Agent Configuration
            st.subheader("Basic Settings")
            
            agent_name = st.text_input(
                "Agent Name",
                value=loaded_config.get('agent', {}).get('name', 'MyAgent') if loaded_config else "MyAgent",
                help="Unique identifier for your agent"
            )
            
            system_prompt = st.text_area(
                "System Prompt",
                value=loaded_config.get('agent', {}).get('system_prompt', 'You are a helpful AI assistant.') if loaded_config else "You are a helpful AI assistant.",
                height=100,
                help="Instructions that define the agent's behavior and personality"
            )
            
            model_options = ["gpt-4o", "gpt-4o-mini", "gpt-4.1","gpt-4.1-mini",'gpt-4.1-nano']
            current_model = loaded_config.get('agent', {}).get('model', 'gpt-4o-mini') if loaded_config else 'gpt-4o-mini'
            model_index = model_options.index(current_model) if current_model in model_options else 1
            
            model = st.selectbox(
                "Model",
                model_options,
                index=model_index,
                help="OpenAI model to use for the agent"
            )
            
            # Server Configuration
            st.subheader("Server Settings")
            
            col_host, col_port = st.columns(2)
            with col_host:
                host = st.text_input("Host", value=loaded_config.get('server', {}).get('host', '0.0.0.0') if loaded_config else "0.0.0.0")
            with col_port:
                port = st.number_input("Port", min_value=1000, max_value=65535, value=loaded_config.get('server', {}).get('port', 8010) if loaded_config else 8010)
            
            # Tool Configuration
            st.subheader("Tools & Capabilities")
            
            # Built-in tools
            with st.expander("Built-in Tools"):
                st.markdown("Enable or disable built-in tools for your agent.")
                
                # Extract current tool settings from loaded config
                current_tools = loaded_config.get('agent', {}).get('capabilities', {}).get('tools', []) if loaded_config else []
                
                enable_web_search = st.checkbox("Web Search", value="web_search" in current_tools if loaded_config else True)
                enable_draw_image = st.checkbox("Image Generation", value="draw_image" in current_tools if loaded_config else False)
            
            # Custom tools
            with st.expander("Custom Tools"):
                st.markdown("Configure custom tools from your toolkit directory.")
                
                toolkit_path = st.text_input(
                    "Toolkit Path",
                    value="toolkit",
                    help="Path to custom toolkit directory containing your custom tools"
                )

                # Extract custom tools from loaded config
                current_custom_tools = []
                if loaded_config:
                    current_tools = loaded_config.get('agent', {}).get('capabilities', {}).get('tools', [])
                    current_custom_tools = [tool for tool in current_tools if tool not in ["web_search", "draw_image"]]
                
                custom_tools = st.text_area(
                    "Custom Tool Names (one per line)",
                    value="\n".join(current_custom_tools) if current_custom_tools else "",
                    help="Enter the names of custom tools from your toolkit",
                    placeholder="calculate_square\nfetch_weather"
                )            

                
                # Add helper info for custom tools
                if custom_tools.strip():
                    st.info("💡 Make sure your custom tools are defined in the toolkit directory and registered in `__init__.py`")
                    with st.expander("Custom Tools Setup Guide"):
                        st.markdown("""
                        **Creating Custom Tools:**
                        1. Create your toolkit directory (e.g., `toolkit/`)
                        2. Add your tool functions in `.py` files
                        3. Register tools in `__init__.py`:
                        ```python
                        # toolkit/__init__.py
                        from .your_tools import calculate_square, fetch_weather
                        
                        TOOLKIT_REGISTRY = {
                            "calculate_square": calculate_square,
                            "fetch_weather": fetch_weather
                        }
                        ```
                        4. Use the `@function_tool()` decorator on your functions
                        """)
            
            # MCP Servers
            with st.expander("MCP Servers"):
                st.markdown("Configure Model Context Protocol servers for dynamic tool loading.")
                
                # Extract current MCP servers from loaded config
                current_mcp_servers = loaded_config.get('agent', {}).get('capabilities', {}).get('mcp_servers', []) if loaded_config else []
                
                mcp_servers = st.text_area(
                    "MCP Server URLs (one per line)",
                    value="\n".join(current_mcp_servers) if current_mcp_servers else "",
                    help="Enter URLs of MCP servers for dynamic tool loading",
                    placeholder="http://localhost:8001/mcp/\nhttp://localhost:8002/mcp/"
                )
            
            
            st.subheader("Advanced Settings")

            # Sub-agents Configuration
            with st.expander("Sub-agents (Multi-Agent System)"):
                st.markdown("Configure specialized sub-agents for hierarchical agent systems.")
                
                with st.container():
                    col_info, col_example = st.columns(2)
                    with col_info:
                        st.info("💡 **How it works:**\n- Main agent coordinates tasks\n- Sub-agents handle specialized work\n- Automatic delegation based on task type")
                    with col_example:
                        st.success("📝 **Example Use Cases:**\n- Research + Writing agents\n- Analysis + Visualization\n- Data Processing + Reporting")
                
                # Load existing sub-agents configuration
                current_sub_agents = loaded_config.get('agent', {}).get('sub_agents', []) if loaded_config else []
                
                sub_agents_enabled = st.checkbox("Enable Sub-agents", value=len(current_sub_agents) > 0 if loaded_config else False)
                
                sub_agents_config = []
                if sub_agents_enabled:
                    default_num = len(current_sub_agents) if current_sub_agents else 2
                    num_sub_agents = st.number_input("Number of Sub-agents", min_value=1, max_value=10, value=default_num)
                    
                    for i in range(num_sub_agents):
                        with st.container():
                            st.write(f"**Sub-agent {i+1}:**")
                            col_name, col_desc = st.columns(2)
                            
                            # Use existing values if available
                            existing_agent = current_sub_agents[i] if i < len(current_sub_agents) else {}
                            
                            with col_name:
                                sub_name = st.text_input(f"Name", 
                                                       key=f"sub_name_{i}", 
                                                       value=existing_agent.get('name', ''),
                                                       placeholder="research_agent")
                            with col_desc:
                                sub_desc = st.text_input(f"Description", 
                                                       key=f"sub_desc_{i}", 
                                                       value=existing_agent.get('description', ''),
                                                       placeholder="Research specialist")
                            sub_url = st.text_input(f"Server URL", 
                                                   key=f"sub_url_{i}", 
                                                   value=existing_agent.get('server_url', ''),
                                                   placeholder="http://localhost:8011")
                            
                            if sub_name and sub_desc and sub_url:
                                sub_agents_config.append({
                                    "name": sub_name,
                                    "description": sub_desc,
                                    "server_url": sub_url
                                })
                            
                            if i < num_sub_agents - 1:  # Don't add divider after last item
                                st.divider()
                    
                    if sub_agents_config:
                        st.success(f"✅ {len(sub_agents_config)} sub-agent(s) configured")
                        with st.expander("� Deployment Order"):
                            st.markdown("""
                            **Start servers in this order:**
                            1. Start all sub-agent servers first
                            2. Wait for them to be healthy
                            3. Start the main coordinator agent
                            
                            **Example:**
                            ```bash
                            # Terminal 1: Start sub-agents
                            xagent-server --config research_agent.yaml
                            
                            # Terminal 2: Start sub-agents  
                            xagent-server --config writing_agent.yaml
                            
                            # Terminal 3: Start coordinator (this agent)
                            xagent-server --config coordinator_agent.yaml
                            ```
                            """)
                    else:
                        st.warning("⚠️ Fill in all sub-agent fields to enable multi-agent system")
            
            # Structured Output Configuration
            with st.expander("Structured Output (Optional)"):
                st.markdown("Define the expected response format using Pydantic models.")
                
                # Load existing structured output configuration
                current_output_schema = loaded_config.get('agent', {}).get('output_schema', {}) if loaded_config else {}
                has_output_schema = bool(current_output_schema)
                
                enable_structured_output = st.checkbox("Enable Structured Output", value=has_output_schema)
                
                if enable_structured_output:
                    class_name = st.text_input("Class Name", 
                                             value=current_output_schema.get('class_name', 'ResponseModel'))
                    
                    st.write("**Fields:**")
                    
                    # Get existing fields
                    existing_fields = current_output_schema.get('fields', {})
                    default_num_fields = len(existing_fields) if existing_fields else 1
                    
                    num_fields = st.number_input("Number of Fields", min_value=1, max_value=20, value=default_num_fields)
                    
                    output_fields = {}
                    existing_field_items = list(existing_fields.items())
                    
                    for i in range(num_fields):
                        with st.container():
                            col_field_name, col_field_type = st.columns([1, 1])
                            
                            # Use existing field data if available
                            existing_field_name = existing_field_items[i][0] if i < len(existing_field_items) else ""
                            existing_field_data = existing_field_items[i][1] if i < len(existing_field_items) else {}
                            
                            with col_field_name:
                                field_name = st.text_input(f"Field {i+1} Name", 
                                                          key=f"field_name_{i}", 
                                                          value=existing_field_name,
                                                          placeholder="title")
                            with col_field_type:
                                # Get existing field type
                                existing_field_type = existing_field_data.get('type', 'str')
                                field_type_options = ["str", "int", "float", "bool", "list"]
                                field_type_index = field_type_options.index(existing_field_type) if existing_field_type in field_type_options else 0
                                
                                field_type = st.selectbox(
                                    f"Type",
                                    field_type_options,
                                    index=field_type_index,
                                    key=f"field_type_{i}"
                                )

                            # For list type, add items specification
                            list_items_type = None
                            if field_type == "list":
                                # Get existing list items type
                                existing_items_type = existing_field_data.get('items', {}).get('type', 'str') if existing_field_data else 'str'
                                items_type_options = ["str", "int", "float", "bool"]
                                items_type_index = items_type_options.index(existing_items_type) if existing_items_type in items_type_options else 0
                                
                                list_items_type = st.selectbox(
                                    f"List Items Type",
                                    items_type_options,
                                    index=items_type_index,
                                    key=f"list_items_{i}",
                                    help="Type of elements in the list"
                                )
                            
                            # Description field (full width)
                            existing_description = existing_field_data.get('description', '') if existing_field_data else ''
                            field_desc = st.text_input(f"Description", 
                                                      key=f"field_desc_{i}", 
                                                      value=existing_description,
                                                      placeholder="Description of the field")
                            

                            if field_name:
                                field_config = {
                                    "type": field_type,
                                    "description": field_desc
                                }
                                
                                # Add items for list type
                                if field_type == "list" and list_items_type:
                                    field_config["items"] = {"type": list_items_type}
                                
                                output_fields[field_name] = field_config
                            
                            if i < num_fields - 1:  # Don't add divider after last field
                                st.divider()
                    
                    # Show preview of generated model
                    if output_fields:
                        st.subheader("📋 Generated Model Preview")
                        
                        # Generate Python code preview
                        python_code = f"from typing import List\nfrom pydantic import BaseModel, Field\n\nclass {class_name}(BaseModel):\n"
                        
                        for field_name, field_config in output_fields.items():
                            field_type_str = field_config["type"]
                            if field_type_str == "list" and "items" in field_config:
                                items_type = field_config["items"]["type"]
                                field_type_str = f"List[{items_type}]"
                            elif field_type_str == "str":
                                field_type_str = "str"
                            elif field_type_str == "int":
                                field_type_str = "int"
                            elif field_type_str == "float":
                                field_type_str = "float"
                            elif field_type_str == "bool":
                                field_type_str = "bool"
                            elif field_type_str == "dict":
                                field_type_str = "dict"
                            
                            python_code += f'    {field_name}: {field_type_str} = Field(description="{field_config["description"]}")\n'
                        
                        st.code(python_code, language="python")
                    else:
                        st.info("👆 Add fields above to see the generated model preview")
        
            # Session Configuration
            st.subheader("Session Settings")
            use_local_session = st.checkbox(
                "Use Local Session", 
                value=loaded_config.get('agent', {}).get('use_local_session', True) if loaded_config else True, 
                help="If unchecked, will use Redis for session persistence (requires REDIS_URL in environment)"
            )

        with col2:
            st.subheader("📋 Configuration Preview")
            
            # Initialize default values for optional variables
            if 'sub_agents_config' not in locals():
                sub_agents_config = []
            if 'enable_structured_output' not in locals():
                enable_structured_output = False
            if 'output_fields' not in locals():
                output_fields = {}
            if 'class_name' not in locals():
                class_name = ""
            
            # Build configuration
            config = self._build_config(
                agent_name, system_prompt, model, host, port,
                enable_web_search, enable_draw_image, custom_tools,
                mcp_servers, use_local_session, toolkit_path,
                sub_agents_config, enable_structured_output,
                output_fields, class_name
            )
            
            # Display YAML preview
            st.code(yaml.dump(config, default_flow_style=False, allow_unicode=True), language="yaml")
            
            # Save and start buttons
            st.subheader("Actions")
            
            # Set default filename based on mode
            if config_mode == "Edit Existing" and 'selected_file' in locals():
                default_filename = selected_file
            else:
                default_filename = f"{agent_name.lower().replace(' ', '_')}_config.yaml"
            
            config_filename = st.text_input(
                "Config Filename",
                value=default_filename
            )
            
            col_save, col_start = st.columns(2)
            
            with col_save:
                save_button_text = "💾 Update Config" if config_mode == "Edit Existing" else "💾 Save Config"
                if st.button(save_button_text, use_container_width=True):
                    config_path = self.config_dir / config_filename
                    with open(config_path, 'w', encoding='utf-8') as f:
                        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
                    success_message = f"Config updated: {config_path}" if config_mode == "Edit Existing" else f"Config saved to {config_path}"
                    st.success(success_message)
                    time.sleep(1)
                    st.rerun()
            
            with col_start:
                if st.button("🚀 Start Server", use_container_width=True):
                    self._start_server(config, config_filename, toolkit_path)
    
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
        try:
            # Check prerequisites
            if not self._check_prerequisites():
                return
                
            # Save config file
            config_path = self.config_dir / config_filename
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
            
            # Build command
            cmd = ["xagent-server", "--config", str(config_path)]
            if toolkit_path and toolkit_path.strip() and toolkit_path != "toolkit":
                cmd.extend(["--toolkit_path", toolkit_path])
            
            # Create log file path
            agent_name_safe = config['agent']['name'].lower().replace(' ', '_').replace('-', '_')
            log_file = self.logs_dir / f"{agent_name_safe}.log"
            
            st.info(f"🚀 Starting server with command: {' '.join(cmd)}")
            st.info(f"📝 Logs will be written to: {log_file}")
            
            # Start server process with log redirection
            with st.spinner("Starting server..."):
                # Open log file for writing
                log_file_handle = None
                try:
                    log_file_handle = open(log_file, 'w', encoding='utf-8')
                    
                    process = subprocess.Popen(
                        cmd,
                        stdout=log_file_handle,
                        stderr=subprocess.STDOUT,  # Redirect stderr to stdout (which goes to log file)
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
                        
                        # Check if process is still alive
                        if process.poll() is not None:
                            # Process has terminated
                            if log_file_handle:
                                log_file_handle.close()
                                log_file_handle = None
                            
                            # Read the log file to show error
                            try:
                                with open(log_file, 'r', encoding='utf-8') as f:
                                    log_content = f.read()
                                if log_content:
                                    st.error(f"**Server log output:**\n```\n{log_content[-2000:]}\n```")  # Show last 2000 chars
                                else:
                                    st.error("Server process terminated unexpectedly with no log output")
                            except Exception as e:
                                st.error(f"❌ Server process terminated unexpectedly. Could not read log file: {e}")
                            
                            st.error("❌ Server process terminated unexpectedly")
                            return
                        
                        # Check if server is responding
                        if self._check_server_health(server_url):
                            # Server is up and running
                            if log_file_handle:
                                log_file_handle.close()
                                log_file_handle = None
                            
                            server_id = f"{config['agent']['name']}_{config['server']['port']}"
                            self.running_servers[server_id] = {
                                "name": config['agent']['name'],
                                "url": server_url,
                                "config_file": config_filename,
                                "log_file": str(log_file),
                                "pid": process.pid,
                                "started_at": datetime.now().isoformat(),
                                "toolkit_path": toolkit_path
                            }
                            self._save_server_registry()
                            
                            st.success(f"✅ Server started successfully!")
                            st.info(f"🌐 Server URL: {server_url}")
                            st.info(f"🔗 Health Check: {server_url}/health")
                            st.info(f"📝 Log File: {log_file}")
                            st.info(f"⏱️ Startup time: {waited_time} seconds")
                            
                            return
                        
                        # Show progress
                        progress_msg = f"⏳ Waiting for server... ({waited_time}/{max_wait_time}s)"
                        st.info(progress_msg)
                    
                    # Timeout reached
                    if log_file_handle:
                        log_file_handle.close()
                        log_file_handle = None
                    st.error("❌ Server startup timeout")
                    
                    # Try to read log file to show any output
                    try:
                        with open(log_file, 'r', encoding='utf-8') as f:
                            log_content = f.read()
                        if log_content:
                            st.error(f"**Log file content:**\n```\n{log_content[-2000:]}\n```")  # Show last 2000 chars
                        else:
                            st.info("No log output available")
                            
                    except Exception as e:
                        st.warning(f"Could not read log file: {e}")
                    
                    # Terminate the process
                    try:
                        process.terminate()
                        time.sleep(2)
                        if process.poll() is None:
                            process.kill()
                    except:
                        pass
                
                finally:
                    # Ensure log file handle is always closed
                    if log_file_handle:
                        try:
                            log_file_handle.close()
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
                    with open(config_file, 'r', encoding='utf-8') as f:
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
                        st.code(yaml.dump(config, default_flow_style=False, allow_unicode=True), language="yaml")
                
                except Exception as e:
                    st.error(f"Error reading config file: {str(e)}")
    
    def _start_server_from_file(self, config_file):
        """Start server from saved configuration file."""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            toolkit_path = "toolkit"  # Default toolkit path
            self._start_server(config, config_file.name, toolkit_path)
            time.sleep(1)
            st.rerun()
        
        except Exception as e:
            st.error(f"Error starting server: {str(e)}")
    
    def render_running_servers(self):
        """Render running servers management page."""
        st.subheader("🚀 Running Servers")
        
        # Clean up dead servers first
        self._cleanup_dead_servers()
        self._cleanup_dead_webchats()
        
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
                
                # Check if this server has a running web chat
                webchat_info = None
                for wc_id, wc_info in self.running_webchats.items():
                    if wc_info.get('agent_server') == server_info['url']:
                        webchat_info = wc_info
                        break
                
                col1, col2, col3, col4, col5 = st.columns([2, 1, 1, 1, 1])
                
                with col1:
                    st.write(f"{status_icon} **{server_info['name']}**")
                    st.write(f"URL: {server_info['url']}")
                    st.write(f"Started: {server_info.get('started_at', 'Unknown')}")
                    log_file = server_info.get('log_file')
                    if log_file:
                        st.write(f"📝 Log: {log_file}")
                    
                    # Show web chat status if exists
                    if webchat_info:
                        webchat_healthy = self._check_webchat_health(webchat_info['url'])
                        webchat_icon = "🟢" if webchat_healthy else "🔴"
                        st.write(f"{webchat_icon} **Web Chat:** {webchat_info['url']}")
                
                with col2:
                    st.write(f"**Status:** {status_text}")
                    st.write(f"**PID:** {server_info.get('pid', 'Unknown')}")
                    if webchat_info:
                        st.write(f"**Chat PID:** {webchat_info.get('pid', 'Unknown')}")
                
                with col3:
                    if st.button("🌐 Open", key=f"open_{server_id}"):
                        st.markdown(f"[Open Server]({server_info['url']}/health)")
                
                with col4:
                    if webchat_info:
                        # Show close web chat button if web chat is running
                        if st.button("❌ Close Chat", key=f"closechat_{server_id}"):
                            self._stop_web_chat(webchat_info)
                            time.sleep(1)
                            st.rerun()
                    else:
                        # Show start web chat button if no web chat is running
                        if st.button("💬 Web Chat", key=f"webchat_{server_id}"):
                            if is_healthy:
                                self._start_web_chat(server_info['url'])
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error("❌ Server is not healthy. Please check server status first.")
                
                with col5:
                    if st.button("🛑 Stop", key=f"stop_{server_id}"):
                        # Also stop web chat if running
                        if webchat_info:
                            self._stop_web_chat(webchat_info)
                        self._stop_server(server_id, server_info)
                        time.sleep(1)
                        st.rerun()
                
                # Server details
                with st.expander(f"Details - {server_info['name']}"):
                    col_details, col_logs = st.columns([1, 1])
                    
                    with col_details:
                        st.subheader("Server Info")
                        st.json(server_info)
                    
                    with col_logs:
                        st.subheader("📝 Log File")
                        log_file = server_info.get('log_file')
                        if log_file and Path(log_file).exists():
                            if st.button(f"📄 View Latest Logs", key=f"viewlog_{server_id}"):
                                try:
                                    with open(log_file, 'r', encoding='utf-8') as f:
                                        log_content = f.read()
                                    
                                    # Show last 3000 characters of log
                                    if len(log_content) > 3000:
                                        display_content = "..." + log_content[-3000:]
                                    else:
                                        display_content = log_content
                                    
                                    st.text_area(
                                        f"Latest logs from {Path(log_file).name}:",
                                        value=display_content,
                                        height=300,
                                        disabled=True,
                                        key=f"logcontent_{server_id}"
                                    )
                                except Exception as e:
                                    st.error(f"Error reading log file: {e}")
                            
                            st.info(f"Log file: `{log_file}`")
                            
                            # Add option to clear logs
                            if st.button(f"🗑️ Clear Logs", key=f"clearlog_{server_id}"):
                                try:
                                    with open(log_file, 'w', encoding='utf-8') as f:
                                        f.write("")  # Clear the file
                                    st.success("✅ Log file cleared")
                                    time.sleep(1)
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error clearing log file: {e}")
                        else:
                            st.warning("No log file found or file doesn't exist")
                
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
    
    def _start_web_chat(self, agent_url):
        """Start web chat interface for the agent server."""
        try:
            # Convert server URL for web chat command
            # Handle different host formats
            if "0.0.0.0" in agent_url:
                # Replace 0.0.0.0 with localhost for web chat
                chat_agent_url = agent_url.replace("0.0.0.0", "localhost")
            else:
                chat_agent_url = agent_url
            
            # Find available port
            try:
                available_port = self._find_available_port(8501)
                st.info(f"🔍 Found available port: {available_port}")
            except Exception as e:
                st.error(f"❌ Could not find available port: {str(e)}")
                return
            
            # Build web chat command
            cmd = [
                "xagent-web",
                "--host", "0.0.0.0",
                "--port", str(available_port), 
                "--agent-server", chat_agent_url
            ]
            
            st.info(f"🚀 Starting Web Chat with command: {' '.join(cmd)}")
            
            # Start web chat process in background
            with st.spinner("Starting Web Chat..."):
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=os.setsid,
                    cwd=os.getcwd(),
                    env=os.environ.copy()
                )
                
                # Wait a moment for startup
                time.sleep(5)
                
                # Check if process is still alive
                if process.poll() is not None:
                    # Process has terminated
                    stdout, stderr = process.communicate()
                    stdout_str = stdout.decode() if stdout else ""
                    stderr_str = stderr.decode() if stderr else ""
                    
                    if stderr_str:
                        st.error(f"**Error output:**\n```\n{stderr_str}\n```")
                    if stdout_str:
                        st.info(f"**Standard output:**\n```\n{stdout_str}\n```")
                    
                    st.error("❌ Web Chat process terminated unexpectedly")
                    return
                
                # Check if web chat is responding
                web_chat_url = f"http://localhost:{available_port}"
                max_wait_time = 15  # Maximum wait time in seconds
                check_interval = 2  # Check every 2 seconds
                waited_time = 0
                
                while waited_time < max_wait_time:
                    if self._check_webchat_health(web_chat_url):
                        # Web Chat is up and running
                        webchat_id = f"webchat_{agent_url.split(':')[-1]}_{available_port}"
                        self.running_webchats[webchat_id] = {
                            "agent_server": agent_url,
                            "url": web_chat_url,
                            "port": available_port,
                            "pid": process.pid,
                            "started_at": datetime.now().isoformat()
                        }
                        self._save_webchat_registry()
                        
                        st.success(f"✅ Web Chat started successfully!")
                        st.info(f"💬 Web Chat URL: {web_chat_url}")
                        st.info(f"🔗 Agent Server: {chat_agent_url}")
                        st.info(f"⏱️ Startup time: {waited_time + 5} seconds")
                        
                        # Add a clickable link
                        st.markdown(f"👉 [Open Web Chat Interface]({web_chat_url})")
                        
                        return
                    
                    time.sleep(check_interval)
                    waited_time += check_interval
                
                # Timeout reached
                st.error("❌ Web Chat startup timeout")
                
                # Terminate the process
                try:
                    process.terminate()
                    time.sleep(2)
                    if process.poll() is None:
                        process.kill()
                except:
                    pass
                
        except FileNotFoundError:
            st.error("❌ `xagent-web` command not found. Please ensure xAgent is properly installed.")
            st.info("Try running: `pip install -e .` in the xAgent directory")
        except Exception as e:
            st.error(f"❌ Error starting Web Chat: {str(e)}")
            import traceback
            st.error(f"**Traceback:**\n```\n{traceback.format_exc()}\n```")
    
    def _stop_web_chat(self, webchat_info):
        """Stop a running web chat."""
        try:
            pid = webchat_info.get('pid')
            port = webchat_info.get('port')
            
            if pid:
                try:
                    # First, try to terminate the entire process group
                    try:
                        # Get the process group ID and terminate the whole group
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                        st.info(f"🔄 Sending SIGTERM to process group {pid}...")
                        time.sleep(3)
                        
                        # Check if process still exists
                        if psutil.pid_exists(pid):
                            st.warning(f"⚠️ Process {pid} still running, force killing...")
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                            time.sleep(2)
                    except ProcessLookupError:
                        # Process group doesn't exist, try individual process
                        os.kill(pid, signal.SIGTERM)
                        time.sleep(3)
                        
                        if psutil.pid_exists(pid):
                            os.kill(pid, signal.SIGKILL)
                            time.sleep(2)
                    
                    # Double check: kill any remaining processes using the port
                    if port:
                        self._kill_processes_on_port(port)
                    
                    # Verify the process is really dead
                    if psutil.pid_exists(pid):
                        st.error(f"❌ Failed to terminate process {pid}")
                    else:
                        st.success(f"✅ Web Chat on port {port} stopped successfully")
                
                except ProcessLookupError:
                    st.info(f"Web Chat on port {port} was already stopped")
                except Exception as e:
                    st.error(f"Error stopping Web Chat: {str(e)}")
                    # Try to kill processes on port as fallback
                    if port:
                        self._kill_processes_on_port(port)
            
            # Remove from registry
            webchat_id_to_remove = None
            for wc_id, wc_info in self.running_webchats.items():
                if wc_info.get('pid') == webchat_info.get('pid'):
                    webchat_id_to_remove = wc_id
                    break
            
            if webchat_id_to_remove:
                del self.running_webchats[webchat_id_to_remove]
                self._save_webchat_registry()
                
            # Final verification: check if port is really free
            if port:
                time.sleep(1)  # Wait a moment for port to be released
                if self._is_port_in_use(port):
                    st.warning(f"⚠️ Port {port} may still be in use")
                else:
                    st.info(f"✅ Port {port} is now available")
        
        except Exception as e:
            st.error(f"Failed to stop Web Chat: {str(e)}")
    
    def _kill_processes_on_port(self, port):
        """Kill all processes using the specified port."""
        try:
            import subprocess
            
            # Find processes using the port
            result = subprocess.run(
                ['lsof', '-ti', f':{port}'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid_str in pids:
                    try:
                        pid = int(pid_str.strip())
                        os.kill(pid, signal.SIGKILL)
                        st.info(f"🔫 Force killed process {pid} using port {port}")
                    except (ValueError, ProcessLookupError):
                        continue
        except FileNotFoundError:
            # lsof not available, try alternative method
            try:
                result = subprocess.run(
                    ['netstat', '-tulpn'],
                    capture_output=True,
                    text=True
                )
                # Parse netstat output to find PIDs using the port
                # This is a more complex parsing, but provides fallback
                lines = result.stdout.split('\n')
                for line in lines:
                    if f':{port}' in line and 'LISTEN' in line:
                        parts = line.split()
                        if len(parts) > 6 and '/' in parts[-1]:
                            try:
                                pid = int(parts[-1].split('/')[0])
                                os.kill(pid, signal.SIGKILL)
                                st.info(f"🔫 Force killed process {pid} using port {port}")
                            except (ValueError, ProcessLookupError):
                                continue
            except:
                st.warning(f"⚠️ Could not find processes using port {port}")
        except Exception as e:
            st.warning(f"⚠️ Error killing processes on port {port}: {str(e)}")
    
    def _is_port_in_use(self, port):
        """Check if a port is currently in use."""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('localhost', port))
                return False
        except OSError:
            return True


def main():
    """Main function to run the Streamlit app."""
    config_ui = AgentConfigUI()
    config_ui.render_main_page()


if __name__ == "__main__":
    main()
