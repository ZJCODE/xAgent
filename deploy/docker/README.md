# docker-xagent-project

## Overview

This is a sample project (a basic scaffold ) demonstrating how to set up and run an HTTP agent using Docker.

The project utilizes the `myxagent` package to create a flexible and powerful agent capable of handling various tasks.

## Getting Started

### Prerequisites

- Docker and Docker Compose installed on your machine.

### Setup

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Update the `.env` file with your specific configuration, including API keys and Redis connection details.

### Building the Docker Image

To build the Docker image, run:

```bash
docker-compose build
```

rebuild with no cache:

```bash
docker-compose build --no-cache
```

### Running the Application

To start the HTTP agent, use:

```bash
docker-compose up
```

This command will start the services defined in `docker-compose.yml`, including the HTTP agent and any dependencies like Redis.

### Accessing the Agent

Once the application is running, you can access the HTTP agent at:

```
http://localhost:8010
```

### Stopping the Application

To stop the application, press `Ctrl+C` in the terminal where the Docker Compose command is running, or run:

```bash
docker-compose down
```

## Usage

You can interact with the HTTP agent using HTTP requests. For example, to send a chat message, you can use `curl`:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456", 
    "user_message": "Hello, how are you?"
  }'
```

## Custom Tools

You can define custom tools in the `my_toolkit/tools.py` file. These tools can be utilized by the agent during its operation.
