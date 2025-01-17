"""
Current Limitations:
1. Streaming only works for one command at a time.
2. It only streams stdout, not stderr.
3. It requires sending the session_id with every request. (This will change.)

Todo:
1. Make it stream stderr too...
"""

import asyncio
import logging
import re
from enum import Enum
from typing import AsyncIterator, Literal, Optional, Union

import aiohttp
import socketio
from aiohttp import ClientConnectorError, ServerDisconnectedError
from pydantic import BaseModel

logger: logging.Logger = logging.getLogger("kubebox")


class CommandMode(str, Enum):
    STREAM = "stream"
    WAIT = "wait"
    BACKGROUND = "background"


class CommandOutput(BaseModel):
    output: str
    type: Literal["stdout", "stderr"]
    process_id: Union[str, int]


class CommandExit(BaseModel):
    exit_code: int
    process_id: Union[str, int]


class CommandResult(BaseModel):
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    finished: bool


class Status(BaseModel):
    running: bool | None


class CommandKilled(BaseModel):
    status: str
    exit_code: Optional[int] = None


class CommandError(BaseModel):
    error: str


class BackgroundProcess(BaseModel):
    process_id: Union[str, int]


class RuntimeResult(BaseModel):
    stdout: list[str]
    stderr: list[str]
    url_tested: str

    def xml_error(self, idx: int) -> str:
        return f"""
<error_{idx}>
<url_tested>
{self.url_tested}
</url_tested>
<console_errors>
{"\n".join([result.strip() for result in self.stderr])}
</console_errors>
</error_{idx}>
""".strip()


class StreamProcess:
    def __init__(self, process_id: int, stream_queue: asyncio.Queue):
        self.process_id = process_id
        self.stream_queue = stream_queue

    async def stream(self) -> AsyncIterator[CommandOutput | CommandExit]:
        while True:
            output = await self.stream_queue.get()
            if isinstance(output, CommandExit):
                yield output
                break
            if isinstance(output, CommandOutput):
                output.output = clean_output(output.output)
            yield output

    def __aiter__(self):
        return self.stream()


class SandboxClient:
    def __init__(self, private_key: str, url: str = "http://localhost:80"):
        self.private_key = private_key
        self.url = url
        self.sio = socketio.AsyncClient()
        self.connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
        self.client_session = aiohttp.ClientSession(connector=self.connector)
        self.sessions = {}
        self.initialized_event = asyncio.Event()
        self.stream_queues: dict[int, asyncio.Queue] = {}
        self.result_event = asyncio.Event()
        self.result_data = None

        # Register event handlers
        self.sio.on("connect", self.on_connect)
        self.sio.on("disconnect", self.on_disconnect)
        self.sio.on("initialized", self.on_initialized)
        self.sio.on("command_output", self.on_command_output)
        self.sio.on("command_exit", self.on_command_exit)
        self.sio.on("command_result", self.on_command_result)
        self.sio.on("status", self.on_status)
        self.sio.on("command_killed", self.on_killed)
        self.sio.on("command_error", self.on_error)

    async def on_connect(self):
        logger.debug("Connected to the server")

    async def on_disconnect(self):
        logger.debug("Disconnected from the server")

    async def on_initialized(self, data):
        logger.debug("Initialization response: %s", data)
        self.initialized_event.set()

    async def on_command_output(self, data):
        data = CommandOutput(**data)
        data.output = clean_output(data.output)
        await self.get_stream_queue(data.process_id).put(data)

    async def on_command_exit(self, data):
        exit_info = CommandExit(**data)
        logger.debug(
            "Command exited with code %s (Process ID: %s)",
            exit_info.exit_code,
            exit_info.process_id,
        )
        await self.stream_queues[exit_info.process_id].put(
            exit_info
        )  # Signal the end of the stream

    async def on_command_result(self, data):
        self.result_data = CommandResult(**data)
        self.result_data.stdout = clean_output(self.result_data.stdout)
        self.result_data.stderr = clean_output(self.result_data.stderr)
        self.result_event.set()

    async def on_status(self, data):
        status = Status(**data)
        logger.debug("Status: %s", status)

    async def on_killed(self, data):
        killed_info = CommandKilled(**data)
        logger.debug("Command killed: %s", killed_info)

    async def on_error(self, data):
        error_info = CommandError(**data)
        error_info.error = clean_output(error_info.error)

    async def connect(self):
        await self.sio.connect(self.url, transports=["websocket"])

    async def ensure_connection(self):
        """Ensure connection is alive, reconnect if needed"""
        if not self.sio.connected:
            try:
                await self.connect()
            except Exception as e:
                logger.error(f"Failed to reconnect: {e}")
                raise

    async def _make_request(
        self, method: str, endpoint: str, max_retries: int = 3, **kwargs
    ) -> aiohttp.ClientResponse:
        """
        Helper method to make requests with connection check and multiple retries
        if we encounter connection-related errors.

        :param method: HTTP method (e.g. "GET", "POST")
        :param endpoint: The service endpoint (relative path) to call
        :param max_retries: Number of times to re-attempt after connection errors
        :param kwargs: Additional parameters passed to the underlying aiohttp request
        :return: aiohttp.ClientResponse
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Ensure our socket.io connection is up
                await self.ensure_connection()

                # Execute the request
                if method.lower() == "get":
                    return await self.client_session.get(
                        f"{self.url}/{endpoint}", **kwargs
                    )
                else:
                    return await self.client_session.post(
                        f"{self.url}/{endpoint}", **kwargs
                    )

            except (ServerDisconnectedError, ClientConnectorError) as e:
                logger.warning(
                    f"Connection error during {method.upper()} {endpoint}: {e}. "
                    f"Attempt {attempt} of {max_retries}."
                )
                # If we still have retries left, wait a bit and retry
                if attempt < max_retries:
                    # Optional short delay before retry
                    await asyncio.sleep(2)
                else:
                    # No more retries left; raise the exception
                    logger.error(
                        f"All {max_retries} retries failed for {method.upper()} {endpoint}."
                    )
                    raise

    async def initialize_session(self, session_id: str, path: str):
        async with await self._make_request(
            "post",
            "initialize",
            json={"session_id": session_id, "path": path},
        ) as response:
            init_response = await response.json()
            self.sessions[session_id] = init_response["session_id"]

        await self.sio.emit("initialize", {"session_id": session_id})
        await self.initialized_event.wait()

    def get_stream_queue(self, process_id: int) -> asyncio.Queue:
        assert isinstance(process_id, int)
        if process_id not in self.stream_queues:
            self.stream_queues[process_id] = asyncio.Queue()
        return self.stream_queues[process_id]

    async def run_command(
        self,
        session_id: str,
        command: str,
        mode: CommandMode = CommandMode.STREAM,
        path: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Union[CommandResult, BackgroundProcess, StreamProcess]:
        async with await self._make_request(
            "post",
            "run_command",
            json={
                "session_id": session_id,
                "command": command,
                "mode": mode,
                "path": path,
                "timeout": timeout,
            },
        ) as response:
            result = await response.json()

        if mode == CommandMode.STREAM:
            process_id = result["process_id"]
            stream_queue = self.get_stream_queue(process_id)
            await self.sio.emit(
                "start_command_stream",
                {"session_id": session_id, "process_id": process_id},
            )
            return StreamProcess(process_id, stream_queue)
        elif mode == CommandMode.WAIT:
            result = CommandResult(**result)
            result.stdout = clean_output(result.stdout)
            result.stderr = clean_output(result.stderr)
            return result
        elif mode == CommandMode.BACKGROUND:
            return BackgroundProcess(process_id=result["process_id"])

    async def kill_all_processes(self, session_id: str):
        async with await self._make_request(
            "post",
            "kill_all_processes",
            json={"session_id": session_id},
        ) as response:
            response_body: dict = await response.json()
            _failed_processes: list[str] = response_body.get("failed_processes", [])
            return response_body.get("success", False)

    async def get_website_console_logs(
        self, session_id: str, full_url: str
    ) -> RuntimeResult:
        async with await self._make_request(
            "post",
            "get_website_console_logs",
            json={
                "session_id": session_id,
                "full_url": full_url,
            },
        ) as response:
            response_body: dict = await response.json()
            logs: list[dict] = response_body.get("logs", [])
            errors: list[dict] = response_body.get("errors", [])

            logs = [
                log["text"]
                for log in logs
                if log["type"] in ["warning", "debug", "info", "log"]
            ]
            errors = [
                error["text"]
                for error in errors
                if error["type"] not in ["warning", "debug", "info", "log"]
            ]
            logs = [clean_output(log) for log in logs]
            errors = [clean_output(error) for error in errors]

            return RuntimeResult(stdout=logs, stderr=errors, url_tested=full_url)

    async def check_status(self, session_id: str, process_id: str) -> Status:
        # Create an event to wait for the status response
        status_event = asyncio.Event()
        status_data = {}

        async def on_status(data):
            nonlocal status_data
            status_data = data
            status_event.set()

        # Temporarily override the on_status handler
        self.sio.on("status", on_status)

        # Emit the check_status event with session_id and process_id
        await self.sio.emit(
            "check_status", {"session_id": session_id, "process_id": process_id}
        )

        # Wait for the status response
        await status_event.wait()

        # Restore the original on_status handler
        self.sio.on("status", self.on_status)

        return Status(**status_data)

    async def kill_command(self, session_id: str, process_id: str) -> CommandKilled:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.url}/kill_command",
                json={"session_id": session_id, "process_id": process_id},
            ) as response:
                result = await response.json()

        # Ensure the result includes a status field
        if "status" not in result:
            result["status"] = "not found"

        return CommandKilled(**result)

    async def disconnect(self):
        # Close any existing client sessions
        for queue in self.stream_queues.values():
            queue.clear()
        self.stream_queues.clear()

        # Close the shared client session
        await self.client_session.close()

        # Disconnect socket.io
        await self.sio.disconnect()
        await self.sio.eio.disconnect()

    async def get_file(self, session_id: str, file_path: str) -> str:
        async with await self._make_request(
            "get",
            "read_file",
            params={"session_id": session_id, "file_path": file_path},
        ) as response:
            if response.status == 200:
                return (await response.json())["content"]
            else:
                raise Exception(f"Failed to get file: {response.status}")

    async def write_file(
        self, session_id: str, file_path: str, content: str, make_dirs: bool = False
    ):
        async with await self._make_request(
            "post",
            "write_file",
            json={
                "session_id": session_id,
                "file_path": file_path,
                "content": content,
                "make_dirs": make_dirs,
            },
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise Exception(f"Failed to write file: {response.status}")

    async def make_dirs(self, session_id: str, file_path: str):
        async with await self._make_request(
            "post",
            "make_dirs",
            json={"session_id": session_id, "file_path": file_path},
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise Exception(f"Failed to make dirs: {response.status}")

    async def delete_file(self, session_id: str, file_path: str):
        async with await self._make_request(
            "post",
            "delete_file",
            json={"session_id": session_id, "file_path": file_path},
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise Exception(f"Failed to delete file: {response.status}")

    async def file_exists(self, session_id: str, file_path: str) -> bool:
        async with await self._make_request(
            "get",
            "file_exists",
            params={"session_id": session_id, "file_path": file_path},
        ) as response:
            if response.status == 200:
                return bool((await response.json())["exists"])
            elif response.status == 404:
                return False
            else:
                raise Exception(f"Failed to check if file exists: {response.status}")

    async def get_all_file_paths(
        self, session_id: str, regexes: list[str] = None
    ) -> list[str]:
        if regexes is None:
            regexes = []
        async with await self._make_request(
            "get",
            "get_all_file_paths",
            params={"session_id": session_id, "regexes": regexes},
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise Exception(f"Failed to get file paths: {response.status}")


if __name__ == "__main__":

    async def main():
        client = SandboxClient(url="http://4.156.80.55:80")
        await client.connect()

        # Initialize a session
        await client.initialize_session("your-session-id-here", "test_path")

        result = await client.run_command(
            "your-session-id-here",
            "git clone https://github.com/speckai/paige-nextjs-chakra-template test_path",
            mode=CommandMode.WAIT,
        )
        print("Result:", result)

        result = await client.run_command(
            "your-session-id-here", "ls -la", mode=CommandMode.WAIT, path="test_path"
        )
        print(result.stdout)

        result = await client.file_exists("your-session-id-here", "test_path/LICENSE")
        print(result)

        result = await client.get_file("your-session-id-here", "test_path/LICENSE")
        print(result)

        result = await client.write_file(
            "your-session-id-here", "test_path/LICENSE", "Hello, World!"
        )
        print(result)

        result = await client.read_file("your-session-id-here", "test_path/LICENSE")
        print(result)

        result = await client.run_command(
            "your-session-id-here",
            "npm install",
            mode=CommandMode.STREAM,
            path="test_path",
        )
        async for output in result:
            print(output.output)

        result = await client.run_command(
            "your-session-id-here",
            "npm run start",
            mode=CommandMode.STREAM,
            path="test_path",
            # timeout=200,
        )
        async for output in result:
            print(output.output)

        return

        # Run a command in STREAM mode
        async for output in await client.run_command(
            "your-session-id-here", "echo Hello, World!", mode=CommandMode.STREAM
        ):
            print("Streamed Output:", output)

        # Run a command in WAIT mode
        for _i in range(10):
            result = await client.run_command(
                "your-session-id-here", "echo HELLO WORLD!", mode=CommandMode.WAIT
            )
            print("Command Result:", result)

        # Run a command in BACKGROUND mode
        background_process = await client.run_command(
            "your-session-id-here",
            "echo Hello, World! && sleep 10",
            mode=CommandMode.BACKGROUND,
        )
        print("Background Process ID:", background_process.process_id)

        # Check the status of the command
        status = await client.check_status(
            "your-session-id-here", background_process.process_id
        )
        print("Status:", status)

        # Kill the command
        killed_info = await client.kill_command(
            "your-session-id-here", background_process.process_id
        )
        print("Killed Info:", killed_info)

        # Example usage of get_file
        try:
            file_content = await client.get_file("your-session-id-here", "LICENSE")
            print("File Content:", file_content)
        except Exception as e:
            print(e)

        # Example usage of get_all_file_paths
        try:
            file_paths = await client.get_all_file_paths("your-session-id-here")
            print("File Paths:", file_paths)
        except Exception as e:
            print(e)

        # Disconnect from the server
        await client.disconnect()

    # Run the main function
    asyncio.run(main())


def clean_output(output: str | None) -> str | None:
    # TODO: This should NEVER be none. Figure out why
    if output is None:
        print("Output is None, check why")
        return None

    ansi_escape = re.compile(r"\x1b\[([0-9]+)(;[0-9]+)*m")
    return ansi_escape.sub("", output)
