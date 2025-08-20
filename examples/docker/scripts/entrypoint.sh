#!/bin/bash

# Load environment variables from .env file
if [ -f .env ]; then
    export $(cat .env | xargs)
fi

# Start the HTTP agent using myxagent
xagent-server --config config/agent.yaml --toolkit_path my_toolkit