# Kubebox: A K8s Sandbox for LLM Agents

A Python library and server solution for running ephemeral code in Kubernetes with real-time streaming capabilities. This repository is split into two main parts:

1. The Python package (client) named "kubebox"
2. The server application named "sandbox," which you can deploy via OpenTofu

## Overview

• The "kubebox" Python package lets you connect to a sandbox deployment and run commands, write files, read files, and stream real-time output.  
• The "sandbox" component is a FastAPI server that runs inside a Kubernetes (K8s) cluster. It receives commands from the client and executes them in isolated pods.

## Features

- Real-time command execution with output streaming
- File system operations (read/write/delete)
- Multiple execution modes (STREAM, WAIT, BACKGROUND)
- Socket.IO-based real-time command output
- OpenTofu-based K8s cluster deployment

## Installation

Install the Python client:

```bash
pip install kubebox
```

## Quick Start

Below is a high-level overview of how to use kubebox and sandbox together. This assumes you already have tofu installed and configured for your cloud provider (Azure in this example).

1. Initialize tofu:

   ```bash
   tofu init
   ```

2. Deploy the cluster and server:

   ```bash
   tofu apply
   ```

3. Use the "kubebox" client in your Python code to spin up a pod and run commands. For example:

   ```python
   import asyncio
   from kubebox import Kubebox, SandboxClient, CommandMode

   async def main():
       # Configure the cluster using your KUBEBOX_CONFIG
       KUBEBOX_CONFIG = "..."
       kubebox = Kubebox(KUBEBOX_CONFIG)

       # Create a pod and service
       pod = kubebox.create_pod("test-pod", username="test-user")
       service = kubebox.create_service("test-pod", username="test-user")

       # Wait until they’re ready
       await pod.wait_until_ready()
       await service.wait_until_ready()
       ip = await service.get_external_ip()

       # Connect client
       client = SandboxClient(private_key="YOUR_PRIVATE_KEY", url=f"http://{ip}")
       await client.connect()

       # Initialize session and stream a command
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

4. Teardown (optional):
   ```bash
   tofu destroy
   ```

## Command Modes

• STREAM: Stream stdout in real-time  
• WAIT: Wait for command completion and return a single combined result  
• BACKGROUND: Run a command in the background

## File Operations

Here’s an example of reading and writing files. These calls happen through the same server used for command execution:

```python
# Read file
content = await client.get_file("session-id", "path/to/file")

# Write file
await client.write_file("session-id", "path/to/file", "content")

# Check if a file exists
exists = await client.file_exists("session-id", "path/to/file")
```

## The Server (sandbox)

The sandbox server is powered by FastAPI and socketio. It listens for client requests and executes them in ephemeral pods:

- Accepts incoming commands and session creation via REST/WebSockets
- Streams command output through Socket.IO
- Handles file reads/writes with paths scoped to each session
- Includes Node.js, Python, Git, and more for a flexible development environment
- Containerized via Docker with Python 3.12

Below you can see in "sandbox/main.py" how the server manages sessions, logs, and spawns processes.

## tofu Deployment

- The tofu configuration is stored primarily in "sandbox/main.tf," referencing Azure as an example.
- tofu resources define the Kubernetes cluster, namespace, Docker registry secrets, a sample deployment, and load-balanced service.
- Customize the resource group, project name, and other environment variables in "sandbox/variables.tf."

## Dockerfiles and Scripts

- "sandbox/Dockerfile" defines the container image that runs the FastAPI server.
- You can build and push to Docker using commands referenced in "sandbox/COMMANDS.md" (though they reference older naming, you can adapt them to “tofu” usage as needed).

## Known Limitations

- Streaming currently only captures stdout (stderr support is in TODO).
- By default, the server’s ports are open (NetworkPolicy can be applied via Kubernetes).
- The code is structured to support only one streamed command at a time per session.

## License

This project is licensed under the terms of the MIT License.
