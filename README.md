# Kubebox, a K8s sandbox for LLM Agents

A Python library for running code in Kubernetes environments with real-time streaming capabilities.

## Overview

Kubebox consists of two main components:

1. **Client Package (`kubebox`)**: A Python client library for interacting with sandbox environments
2. **Server (`sandbox`)**: A FastAPI server that runs in K8s and executes commands

## Features

- Real-time command execution with output streaming
- File system operations (read/write/delete)
- Multiple execution modes (stream/wait/background)
- Socket.IO-based real-time communication
- Terraform-based K8s cluster deployment
- Support for multiple programming environments (Node.js, Python, etc.)

## Installation

```bash
pip install kubebox
```

## Quick Start

1. Initialize Terraform

```bash
terraform init
```

2. Deploy to Azure K8s

```bash
terraform apply
```

3. Use the client:

```python
import asyncio
from kubebox import Kubebox, SandboxClient, CommandMode


async def main():
    # Initialize Kubebox with your cluster config
    kubebox = Kubebox(KUBEBOX_CONFIG)
    # Create pod and service
    pod = kubebox.create_pod("test-pod", username="test-user")
    service = kubebox.create_service("test-pod", username="test-user")
    # Wait for deployment
    await pod.wait_until_ready()
    await service.wait_until_ready()
    # Get external IP
    ip = await service.get_external_ip()
    # Connect client
    client = SandboxClient(f"http://{ip}")
    await client.connect()
    # Initialize session and run commands
    await client.initialize_session("test-session", "workspace")
    result = await client.run_command(
        "test-session",
        "echo 'Hello World'",
        mode=CommandMode.STREAM
    )

    async for output in result:
        print(output.output)

asyncio.run(main())
```

## Command Modes

- `STREAM`: Stream output in real-time
- `WAIT`: Wait for command completion and return result
- `BACKGROUND`: Run command in background

## File Operations

```python
# Read file
content = await client.get_file("session-id", "path/to/file")
# Write file
await client.write_file("session-id", "path/to/file", "content")
# Check if file exists
exists = await client.file_exists("session-id", "path/to/file")
```

## Server Features

The sandbox server provides:

- WebSocket connections via Socket.IO
- Command execution in isolated environments
- File system management
- Process management and streaming
- Support for multiple programming environments

## Development

The sandbox server is containerized using Docker and includes:

- Python 3.12
- Node.js with npm, pnpm, yarn, and bun
- Git and common development tools
- Chromium for web testing

## License

This project is licensed under the terms of the MIT License.
