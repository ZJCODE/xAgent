# Configuration Reference

This document provides comprehensive details on configuring xAgent through YAML configuration files. Configuration files allow you to define agent behavior, capabilities, server settings, and advanced features without writing code.

## 📋 Table of Contents

- [Basic Configuration](#basic-configuration)
- [Agent Configuration](#agent-configuration)
- [Server Configuration](#server-configuration)
- [Multi-Agent System Configuration](#multi-agent-system-configuration)
- [Structured Output Schema](#structured-output-schema)
- [Tool and MCP Configuration](#tool-and-mcp-configuration)
- [Message Storage Configuration](#message-storage-configuration)
- [Complete Examples](#complete-examples)

## Basic Configuration

### Minimal Configuration

The simplest configuration file for a basic agent:

```yaml
agent:
  name: "MyAgent"
  system_prompt: "You are a helpful AI assistant."
  model: "gpt-4.1-mini"

server:
  host: "0.0.0.0"
  port: 8010
```

### Standard Configuration

A more comprehensive setup with common features:

```yaml
agent:
  name: "Assistant"
  system_prompt: |
    You are a helpful AI assistant with access to web search and image generation.
    Always provide accurate and helpful responses.
  model: "gpt-4.1-mini"
  
  capabilities:
    tools:
      - "web_search"      # Built-in web search
      - "draw_image"      # Built-in image generation
    mcp_servers: []       # MCP server URLs (optional)
  
  message_storage: "local" # or "redis"

server:
  host: "0.0.0.0"
  port: 8010
```

## Agent Configuration

### Core Agent Settings

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Agent identifier (used in logs and multi-agent systems) |
| `system_prompt` | string | Yes | Instructions defining agent behavior and personality |
| `model` | string | Yes | OpenAI model to use (e.g., "gpt-4.1-mini", "gpt-4.1") |

### Advanced Agent Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `capabilities` | object | `{}` | Tools, MCP servers, and sub-agents configuration |
| `message_storage` | string | `"local"` | Message persistence: "local" or "redis" |
| `output_schema` | object | `null` | Structured output schema definition |


## Server Configuration

```yaml
server:
  host: "0.0.0.0"    # Listen on all interfaces
  port: 8010         # Port number
```

## Multi-Agent System Configuration

### Hierarchical Multi-Agent Setup (Agent as Tool)

Create a coordinator agent that delegates to specialized sub-agents:

#### Coordinator Agent (coordinator.yaml)

```yaml
agent:
  name: "Coordinator"
  system_prompt: |
    You are a coordinator agent that delegates tasks to specialists.
    - For research tasks, use the research_agent
    - For writing tasks, use the write_agent
    - Always choose the most appropriate specialist for each task
  model: "gpt-4.1"

  capabilities:
    tools: []
    sub_agents:
      - name: "research_agent"
        description: "Research specialist for information gathering"
        server_url: "http://localhost:8011"
      - name: "write_agent"
        description: "Writing specialist for content creation"
        server_url: "http://localhost:8012"

  message_storage: "local"

server:
  host: "0.0.0.0"
  port: 8010
```

#### Research Specialist (research.yaml)

```yaml
agent:
  name: "Research Agent"
  system_prompt: |
    You are a research specialist focused on gathering accurate information.
    - Use web search to find current and reliable sources
    - Verify information from multiple sources when possible
    - Present findings in a clear, structured format
    - Always cite your sources
  model: "gpt-4.1-mini"

  capabilities:
    tools:
      - "web_search"

  message_storage: "local"

server:
  host: "0.0.0.0"
  port: 8011
```

#### Writing Specialist (writer.yaml)

```yaml
agent:
  name: "Writing Agent"
  system_prompt: |
    You are a professional writer specializing in clear communication.
    - Create engaging, well-structured content
    - Adapt tone and style to the intended audience
    - Ensure proper grammar and readability
    - Organize information logically
  model: "gpt-4.1-mini"

  capabilities:
    tools: []

  message_storage: "local"

server:
  host: "0.0.0.0"
  port: 8012
```

### Starting Multi-Agent Systems

```bash
# Start sub-agents first (bottom-up approach)
xagent-server --config research.yaml > logs/research_agent.log 2>&1 &
xagent-server --config writer.yaml > logs/writing_agent.log 2>&1 &

# Start coordinator agent
xagent-server --config coordinator.yaml > logs/coordinator_agent.log 2>&1 &

# Verify all agents are running
curl http://localhost:8010/health  # Coordinator
curl http://localhost:8011/health  # Research agent
curl http://localhost:8012/health  # Writing agent
```

## Structured Output Schema

Define Pydantic models directly in configuration for structured responses:

### Schema Configuration Format

```yaml
agent:
  output_schema:
    class_name: "ResponseModel"  # Generated class name
    fields:
      field_name:
        type: "field_type"       # str, int, float, bool, list
        description: "Field description"
      list_field:
        type: "list"
        items: "str"             # Required for list fields
        description: "List description"
```

### Supported Field Types

- `str` - String values
- `int` - Integer numbers
- `float` - Decimal numbers
- `bool` - Boolean true/false
- `list` - Arrays (requires `items` field)

### Example: Content Report Schema

```yaml
agent:
  name: "ContentAgent"
  system_prompt: "Generate structured content reports."
  model: "gpt-4o-mini"
  
  capabilities:
    tools:
      - "web_search"
      - "draw_image"
  
  output_schema:
    class_name: "ContentReport"
    fields:
      title:
        type: "str"
        description: "Report title"
      summary:
        type: "str"
        description: "Executive summary"
      key_points:
        type: "list"
        items: "str"
        description: "List of key findings"
      confidence_score:
        type: "float"
        description: "Confidence in findings (0.0-1.0)"
      needs_review:
        type: "bool"
        description: "Whether human review is needed"

server:
  host: "0.0.0.0"
  port: 8010
```

### Example: Analysis Result Schema

```yaml
agent:
  output_schema:
    class_name: "AnalysisResult"
    fields:
      analysis_type:
        type: "str"
        description: "Type of analysis performed"
      findings:
        type: "list"
        items: "str"
        description: "List of key findings"
      recommendations:
        type: "list"
        items: "str"
        description: "Recommended actions"
      risk_level:
        type: "str"
        description: "Risk assessment (low/medium/high)"
      confidence:
        type: "float"
        description: "Analysis confidence score"
```

## Tool and MCP Configuration

### Built-in Tools

```yaml
agent:
  capabilities:
    tools:
      - "web_search"    # Web search functionality
      - "draw_image"    # AI image generation
```

### MCP Server Integration

```yaml
agent:
  capabilities:
    mcp_servers:
      - "http://localhost:8001/mcp/"
      - "http://localhost:8002/mcp/"
```

### Custom Tool Directory

When starting the server, specify custom tools:

```bash
xagent-server --config agent.yaml --toolkit_path my_custom_tools/
```

## Message Storage Configuration

### Local Storage (Default)

```yaml
agent:
  message_storage: "local"
```

Stores conversation history in local memory (lost on restart).

### Redis Storage

```yaml
agent:
  message_storage: "redis"
```

Requires Redis server and `REDIS_URL` environment variable.

## Complete Examples

### Production Web Agent

```yaml
agent:
  name: "WebAssistant"
  system_prompt: |
    You are a professional web assistant with access to real-time information.
    
    Capabilities:
    - Search the web for current information
    - Generate images when requested
    - Provide detailed, accurate responses
    
    Guidelines:
    - Always verify information from multiple sources
    - Cite sources when providing factual information
    - Be concise but comprehensive
    - Ask for clarification when requests are ambiguous
  
  model: "gpt-4.1-mini"
  
  capabilities:
    tools:
      - "web_search"
      - "draw_image"
    mcp_servers: []
  
  message_storage: "redis"

server:
  host: "0.0.0.0"
  port: 8010
```

### Research and Analysis Agent

```yaml
agent:
  name: "ResearchAnalyst"
  system_prompt: |
    You are a research analyst specializing in comprehensive information gathering and analysis.
    
    Your process:
    1. Gather information from multiple reliable sources
    2. Cross-reference and verify facts
    3. Analyze trends and patterns
    4. Present findings in structured format
    
    Always provide:
    - Source citations
    - Confidence levels
    - Limitations of the analysis
  
  model: "gpt-4.1"
  
  capabilities:
    tools:
      - "web_search"
  
  output_schema:
    class_name: "ResearchReport"
    fields:
      topic:
        type: "str"
        description: "Research topic"
      executive_summary:
        type: "str"
        description: "Brief overview of findings"
      detailed_findings:
        type: "list"
        items: "str"
        description: "Detailed research findings"
      sources:
        type: "list"
        items: "str"
        description: "List of sources consulted"
      confidence_level:
        type: "str"
        description: "Overall confidence in findings"
      recommendations:
        type: "list"
        items: "str"
        description: "Action recommendations"
  
  message_storage: "redis"

server:
  host: "0.0.0.0"
  port: 8010
```

### Multi-Modal Creative Agent

```yaml
agent:
  name: "CreativeAssistant"
  system_prompt: |
    You are a creative assistant that combines text and visual content.
    
    Specializations:
    - Creative writing and storytelling
    - Visual content creation through AI image generation
    - Multi-modal content strategies
    
    When creating content:
    - Consider both text and visual elements
    - Ensure coherence between different media
    - Adapt style to the intended audience and purpose
  
  model: "gpt-4.1-mini"
  
  capabilities:
    tools:
      - "web_search"
      - "draw_image"
  
  output_schema:
    class_name: "CreativeContent"
    fields:
      content_type:
        type: "str"
        description: "Type of content created"
      text_content:
        type: "str"
        description: "Written content"
      image_descriptions:
        type: "list"
        items: "str"
        description: "Descriptions of generated images"
      style_notes:
        type: "str"
        description: "Style and tone considerations"
      target_audience:
        type: "str"
        description: "Intended audience"
  
  message_storage: "local"

server:
  host: "0.0.0.0"
  port: 8010
```

---