"""DockerSandbox — Docker 容器化隔离执行"""

import asyncio
import codecs
import os
import uuid
from contextlib import suppress

from .base import OutputCallback, Sandbox
from .local import SHELL_ENVIRONMENTS
from ..runtime.process_env import hidden_process_creationflags


class DockerSandbox(Sandbox):
    """Docker 沙箱——临时容器隔离执行代码"""

    IMAGE = "python:3.12-slim"
    _image_ready: set[tuple[str, str]] = set()
    _image_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def __init__(self, timeout: int = 60, workdir: str = ".", memory_limit: str = "512m",
                 network: bool = False, docker_cmd: str = "docker",
                 reuse_container: bool = False, container_name: str | None = None,
                 image: str | None = None):
        self.timeout = timeout
        self.workdir = os.path.abspath(workdir)
        self.memory_limit = memory_limit
        self.network = network
        self.docker_cmd = docker_cmd
        self.reuse_container = reuse_container
        self.container_name = container_name or f"agent-sandbox-{uuid.uuid4().hex[:12]}"
        # Keep the small upstream image as the safe default, while allowing a
        # locally built project image to provide pytest and other dependencies.
        self.image = (image or os.getenv("ASTRA_DOCKER_IMAGE", "")).strip() or self.IMAGE

    async def _ensure_image(self):
        key = (self.docker_cmd, self.image)
        if key in self._image_ready:
            return

        lock = self._image_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._image_ready:
                return
            proc = await asyncio.create_subprocess_exec(
                self.docker_cmd, "image", "inspect", self.image,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=hidden_process_creationflags(),
            )
            await self._communicate_startup_process(proc)
            if proc.returncode != 0:
                pull = await asyncio.create_subprocess_exec(
                    self.docker_cmd, "pull", self.image,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=hidden_process_creationflags(),
                )
                await self._communicate_startup_process(pull)
                if pull.returncode != 0:
                    raise RuntimeError(f"Failed to pull Docker image {self.image}")
            self._image_ready.add(key)

    @staticmethod
    async def _communicate_startup_process(proc: asyncio.subprocess.Process) -> None:
        """Do not leave image inspect/pull running when startup is cancelled."""
        try:
            await proc.communicate()
        except asyncio.CancelledError:
            if proc.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    proc.kill()
                with suppress(ProcessLookupError, OSError):
                    await proc.wait()
            raise

    def _build_args(self, command: str, name: str = "") -> list[str]:
        return [
            self.docker_cmd, "run", "--rm", "-i",
            *(["--name", name] if name else []),
            "--workdir", "/workspace",
            "-v", f"{self.workdir}:/workspace",
            "--memory", self.memory_limit,
            "--network", "none" if not self.network else "bridge",
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",
            "--pids-limit", "100",
            self.image,
            "sh", "-c", command,
        ]

    def _container_options(self) -> list[str]:
        return [
            "--workdir", "/workspace",
            "-v", f"{self.workdir}:/workspace",
            "--memory", self.memory_limit,
            "--network", "none" if not self.network else "bridge",
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",
            "--pids-limit", "100",
        ]

    def _build_start_args(self) -> list[str]:
        return [
            self.docker_cmd, "run", "-d", "-i", "--name", self.container_name,
            *self._container_options(),
            self.image,
            "sh",
        ]

    def _build_exec_args(self, command: str) -> list[str]:
        return [self.docker_cmd, "exec", "-i", self.container_name, "sh", "-c", command]

    @staticmethod
    async def _collect_stream(
        reader: asyncio.StreamReader | None,
        stream: str,
        on_output: OutputCallback | None,
    ) -> bytes:
        if reader is None:
            return b""
        chunks: list[bytes] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if on_output is not None:
                text = decoder.decode(chunk, final=False)
                if text:
                    on_output(stream, text)
        if on_output is not None:
            tail = decoder.decode(b"", final=True)
            if tail:
                on_output(stream, tail)
        return b"".join(chunks)

    async def _remove_container(self, name: str) -> None:
        """Remove a container whose client was stopped, and with it everything it runs."""
        try:
            cleanup = await asyncio.create_subprocess_exec(
                self.docker_cmd, "rm", "-f", name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=hidden_process_creationflags(),
            )
            await asyncio.wait_for(cleanup.communicate(), timeout=5)
        except (asyncio.TimeoutError, OSError):
            pass

    async def _exec_args(
        self,
        args: list[str],
        on_output: OutputCallback | None = None,
        *,
        container: str = "",
    ) -> dict:
        """Run one docker command. ``container`` names a one-shot container to remove when the run is stopped."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=hidden_process_creationflags(),
            )
            stdout_task = asyncio.create_task(self._collect_stream(proc.stdout, "stdout", on_output))
            stderr_task = asyncio.create_task(self._collect_stream(proc.stderr, "stderr", on_output))
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.timeout)
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                # Killing the client does not stop the container it started.
                if container:
                    await self._remove_container(container)
                # Keep what the run wrote before it was stopped.
                collected = await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                stdout, stderr = (item if isinstance(item, bytes) else b"" for item in collected)
                error = stderr.decode("utf-8", errors="replace").strip()
                notice = f"[Timeout] Docker execution exceeded {self.timeout}s"
                return {
                    "output": stdout.decode("utf-8", errors="replace").strip(),
                    "error": f"{error}\n{notice}" if error else notice,
                    "exit_code": -1,
                    "timed_out": True,
                }
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                if container:
                    await self._remove_container(container)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise
            return {
                "output": stdout.decode("utf-8", errors="replace").strip(),
                "error": stderr.decode("utf-8", errors="replace").strip(),
                "exit_code": proc.returncode or 0,
            }
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return {"output": "", "error": f"[Timeout] Docker execution exceeded {self.timeout}s", "exit_code": -1}
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise

    async def _ensure_container(self):
        proc = await asyncio.create_subprocess_exec(
            self.docker_cmd, "container", "inspect", self.container_name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=hidden_process_creationflags(),
        )
        await proc.communicate()
        if proc.returncode == 0:
            return

        result = await self._exec_args(self._build_start_args())
        if result["exit_code"] != 0:
            raise RuntimeError(result["error"] or "failed to start reusable Docker sandbox")

    async def _exec(
        self,
        command: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        if self.reuse_container:
            await self._ensure_container()
            return await self._exec_args(self._build_exec_args(command), on_output=on_output)
        # Named, so that a run stopped at the limit or cancelled can be removed.
        name = f"agent-sandbox-run-{uuid.uuid4().hex[:12]}"
        return await self._exec_args(
            self._build_args(command, name), on_output=on_output, container=name,
        )

    async def execute_python(self, code: str) -> dict:
        return await self.execute_python_stream(code)

    async def execute_python_stream(
        self,
        code: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        await self._ensure_image()
        tag = uuid.uuid4().hex[:8]
        host_file = os.path.join(self.workdir, f".sandbox_{tag}.py")
        try:
            with open(host_file, "w", encoding="utf-8") as f:
                f.write(code)
            return await self._exec(
                f"python3 /workspace/.sandbox_{tag}.py",
                on_output=on_output,
            )
        finally:
            if os.path.exists(host_file):
                os.remove(host_file)

    async def execute_shell(self, command: str, environment: str = "auto") -> dict:
        return await self.execute_shell_stream(command, environment=environment)

    async def execute_shell_stream(
        self,
        command: str,
        environment: str = "auto",
        on_output: OutputCallback | None = None,
    ) -> dict:
        requested = environment.strip().lower()
        if requested not in SHELL_ENVIRONMENTS:
            raise ValueError("Shell environment must be one of: auto, windows, wsl, posix")
        if requested == "windows":
            return {
                "output": "",
                "error": "Windows shell execution is unavailable inside DockerSandbox. Use environment='auto' or environment='posix' for the Linux container or switch /sandbox off.",
                "exit_code": -1,
            }
        if requested == "wsl":
            return {
                "output": "",
                "error": "WSL host execution is unavailable inside DockerSandbox. Switch /sandbox off before using environment='wsl'.",
                "exit_code": -1,
            }
        await self._ensure_image()
        return await self._exec(command, on_output=on_output)

    async def close(self):
        if not self.reuse_container:
            return
        await self._exec_args([self.docker_cmd, "rm", "-f", self.container_name])
