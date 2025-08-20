FROM python:3.12-slim

RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources

# Set the working directory
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Set script permissions using absolute path
RUN chmod +x /app/scripts/entrypoint.sh

# Expose the port the app runs on
EXPOSE 8010

# Set the entrypoint using absolute path
ENTRYPOINT ["/app/scripts/entrypoint.sh"]