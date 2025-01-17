import asyncio
import os

from dotenv import load_dotenv

# pip install kubebox
from kubebox import (
    CommandExit,
    CommandMode,
    CommandResult,
    Kubebox,
    KubeboxPod,
    KubeboxService,
    SandboxClient,
    StreamProcess,
)

load_dotenv()
KUBEBOX_CONFIG = os.getenv("KUBEBOX_CONFIG")
KUBEBOX_PUBLIC_KEY = os.getenv("KUBEBOX_PUBLIC_KEY")
KUBEBOX_PRIVATE_KEY = os.getenv("KUBEBOX_PRIVATE_KEY")


async def main():
    # Create Kubebox pod
    print("# Creating Kubebox pod...")
    kubebox = Kubebox(KUBEBOX_CONFIG)
    kubebox.create_secret(
        "kubebox-public-key",
        "default",
        {"KUBEBOX_PUBLIC_KEY": KUBEBOX_PUBLIC_KEY},
    )

    pod: KubeboxPod = kubebox.create_pod(
        pod_name="test-pod",
        username="test-user",
        ports=[3000],
        kubebox_public_key_secret_name="kubebox-public-key",
        kubebox_public_key_key="KUBEBOX_PUBLIC_KEY",
    )

    # Create service
    print("\n\n# Creating Kubebox service...")
    service: KubeboxService = kubebox.create_service(
        pod_name="test-pod",
        username="test-user",
        ports=[3000],
    )

    # Wait for pod and service to be ready
    print("\n\n# Waiting for pod and service to be ready...")
    await asyncio.gather(pod.wait_until_ready(), service.wait_until_ready())
    await asyncio.sleep(1)

    # Get external IP
    ip: str = await service.get_external_ip()
    print("\n\n# External IP:")
    print(f"http://{ip}")

    # Create client
    client = SandboxClient(private_key=KUBEBOX_PRIVATE_KEY, url=f"http://{ip}")
    await client.connect()
    await client.initialize_session("test-session", "personal-site")

    # Clone repo
    await client.run_command(
        "test-session",
        "git clone https://github.com/raghavpillai/personal-site.git personal-site",
        mode=CommandMode.WAIT,
    )

    # List files
    ls_result: CommandResult = await client.run_command(
        "test-session", "ls", mode=CommandMode.WAIT, path="personal-site"
    )
    print("\n\n# Listing files in personal-site:")
    print(ls_result.stdout.strip())

    # Install dependencies and stream
    install_result: StreamProcess = await client.run_command(
        "test-session",
        "yarn install",
        mode=CommandMode.STREAM,
        path="personal-site",
    )
    print("\n\n# Installing dependencies:")
    async for output in install_result:  # Either CommandOutput or CommandExit
        if isinstance(output, CommandExit):
            break
        print(output.output.strip())

    # Start server and stream
    start_result: StreamProcess = await client.run_command(
        "test-session",
        "yarn dev",
        mode=CommandMode.STREAM,
        path="personal-site",
    )
    print(f"\n\n# Starting server: http://{ip}:3000")
    async for output in start_result:
        print(output.output.strip())


if __name__ == "__main__":
    asyncio.run(main())
