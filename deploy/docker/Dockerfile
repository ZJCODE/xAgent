FROM python:3.12-slim-bookworm

# Set the working directory
WORKDIR /app

# Install myxagent directly
RUN pip install --no-cache-dir myxagent

# Copy the application code
COPY . .

# Set script permissions using absolute path
RUN chmod +x /app/scripts/entrypoint.sh

# Expose the port the app runs on
EXPOSE 8010

# Set the entrypoint using absolute path
ENTRYPOINT ["/app/scripts/entrypoint.sh"]