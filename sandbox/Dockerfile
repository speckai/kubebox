# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Install git, npm, pnpm, yarn, bun, ps, curl, and other development tools
RUN apt-get update && \
    apt-get install -y git npm procps curl wget unzip && \
    npm install -g pnpm yarn && \
    curl -fsSL https://bun.sh/install | bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install uv
ADD https://astral.sh/uv/install.sh ./uv-installer.sh
RUN sh ./uv-installer.sh && rm ./uv-installer.sh
ENV PATH="/root/.local/bin:$PATH"

# Copy the pyproject.toml and uv.lock files to the working directory
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen

# Install chromium
RUN uv run playwright install chromium --with-deps

# Copy the current directory contents into the container at /app
COPY sandbox/ ./sandbox/

# Make port 80 available to the world outside this container
EXPOSE 80
EXPOSE 0-65535

# Define environment variable for Python
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["uv", "run", "uvicorn", "sandbox.main:app", "--host", "0.0.0.0", "--port", "80"]
