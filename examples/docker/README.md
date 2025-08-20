# docker-xagent-project

## Overview

This project sets up an HTTP agent using the `myxagent` package, leveraging Docker for easy deployment and management. The agent is capable of processing requests and interacting with various tools defined in the project.

## Project Structure

The project consists of the following files and directories:

- **docker-compose.yml**: Defines the services, networks, and volumes for the Docker application.
- **Dockerfile**: Contains instructions to build the Docker image for the application.
- **requirements.txt**: Lists the Python dependencies required for the project.
- **.env.example**: Provides an example of environment variables needed for the application.
- **.env**: Contains environment variables used by the application.
- **.dockerignore**: Specifies files and directories to ignore when building the Docker image.
- **config/agent.yaml**: Configuration settings for the `myxagent` HTTP agent.
- **my_toolkit/**: Contains custom tools for the agent.
- **scripts/**: Includes entry point and utility scripts for the Docker container.
- **README.md**: Documentation for the project.

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

## Contributing

Contributions are welcome! Please follow the standard Git workflow for submitting changes.

## License

This project is licensed under the MIT License. See the LICENSE file for details.