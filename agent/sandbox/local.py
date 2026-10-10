"""LocalSandbox — 本地受限执行环境"""

import asyncio
import codecs
import locale
import os
import re
import signal
import shlex
import sys
import uuid
from pathlib import Path

from ..runtime.process_env import hidden_process_creationflags
from ..runtime.self_protection import HostProcessGuard
from .windows_job import WindowsJob, asyncio_process_handle
from .base import OutputCallback, Sandbox
from .wsl_pty import PersistentWslShell


DANGEROUS_PATTERNS = [
    "dd if=",
    "mkfs", "fdisk", "mkswap",
    ":(){",
    "> /dev/sda", "> /dev/nvme",
    "chmod 777 /", "chown ",
]

# dsh's persistent bash tool gives one command a five-minute budget. Keep the
# ordinary host shell timeout independent: Minimal's persistent Bash needs
# enough room for real builds/tests on every supported host.
_DEFAULT_PERSISTENT_BASH_TIMEOUT = 300
_DEFAULT_PERSISTENT_WSL_TIMEOUT = _DEFAULT_PERSISTENT_BASH_TIMEOUT

# Killing a timed-out process normally closes its pipes at once. A descendant
# that survives the kill can keep them open, so the wait for the last bytes is
# bounded instead of lasting until that descendant exits.
_STOPPED_OUTPUT_WAIT_SECONDS = 1.0

SHELL_ENVIRONMENTS = {"auto", "windows", "wsl", "posix"}
WSL_COMMANDS = {
    "awk", "bash", "cat", "chmod", "chown", "cp", "df", "du", "find",
    "grep", "head", "kill", "less", "ls", "mkdir", "mv", "ps", "pwd",
    "rm", "sed", "sh", "tail", "touch", "uname", "which", "xargs",
}


class SandboxError(Exception):
    pass


class _TimeLimitExceeded(asyncio.TimeoutError):
    """The time limit expired; holds what the process wrote before it was stopped."""

    def __init__(self, stdout: bytes, stderr: bytes):
        super().__init__()
        self.stdout = stdout
        self.stderr = stderr


# Strong references: the event loop keeps only weak ones to running tasks.
_DISCARDING: set[asyncio.Task] = set()


async def _discard_stream(reader: asyncio.StreamReader) -> None:
    """Read a pipe to its end and keep nothing."""
    try:
        while await reader.read(65536):
            pass
    except Exception:
        return


class LocalSandbox(Sandbox):
    def __init__(
        self,
        timeout: int = 30,
        workdir: str = ".",
        max_output_bytes: int = 200_000,
        max_memory_mb: int | None = None,
        max_cpu_seconds: int | None = None,
        windows_job_containment: bool | None = None,
        windows_job_max_processes: int | None = None,
    ):
        self.timeout = timeout
        self.persistent_bash_timeout = self._resolve_persistent_bash_timeout(timeout)
        # Compatibility for integrations that still inspect the WSL-named
        # configuration field.
        self.persistent_wsl_timeout = self.persistent_bash_timeout
        self.workdir = workdir
        self.max_output_bytes = max_output_bytes
        self.max_memory_mb = max_memory_mb
        self.max_cpu_seconds = max_cpu_seconds
        if windows_job_containment is None:
            windows_job_containment = os.getenv("ASTRA_WINDOWS_JOB_CONTAINMENT", "off").strip().lower() in {
                "1", "true", "yes", "on",
            }
        self.windows_job_containment = bool(windows_job_containment)
        configured_processes = windows_job_max_processes
        if configured_processes is None:
            configured_processes = int(os.getenv("ASTRA_WINDOWS_JOB_MAX_PROCESSES", "64"))
        if configured_processes < 1:
            raise ValueError("windows_job_max_processes must be positive")
        self.windows_job_max_processes = configured_processes
        self._persistent_bash_shell: PersistentWslShell | None = None
        self.process_guard = HostProcessGuard()

    @staticmethod
    def _resolve_persistent_bash_timeout(timeout: int | float | None) -> int | float | None:
        configured = (
            os.getenv("ASTRA_PERSISTENT_BASH_TIMEOUT", "").strip()
            or os.getenv("ASTRA_WSL_PERSISTENT_TIMEOUT", "").strip()
        )
        if configured:
            try:
                value = float(configured)
            except ValueError:
                value = 0.0
            if value > 0:
                return int(value) if value.is_integer() else value
        if timeout is None:
            return None
        return max(timeout, _DEFAULT_PERSISTENT_BASH_TIMEOUT)

    @staticmethod
    def _resolve_persistent_wsl_timeout(timeout: int | float | None) -> int | float | None:
        """Compatibility alias for the historic WSL-specific timeout resolver."""
        return LocalSandbox._resolve_persistent_bash_timeout(timeout)

    async def _attach_windows_job(self, proc: asyncio.subprocess.Process) -> WindowsJob | None:
        if sys.platform != "win32" or not self.windows_job_containment:
            return None
        try:
            return WindowsJob.create(
                process_handle=asyncio_process_handle(proc),
                memory_mb=self.max_memory_mb,
                cpu_seconds=self.max_cpu_seconds,
                max_processes=self.windows_job_max_processes,
            )
        except Exception as exc:
            # Opt-in containment is a safety promise: fail closed if Windows
            # refuses assignment (for example because of a restrictive parent job).
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise SandboxError(f"Windows Job containment could not be applied: {exc}") from exc

    def _limit_kwargs(self) -> dict:
        if sys.platform == "win32":
            # The backend itself runs windowless; without CREATE_NO_WINDOW each
            # spawned python.exe/wsl.exe/cmd.exe would flash a console window.
            return {"creationflags": hidden_process_creationflags()}
        if self.max_memory_mb is None and self.max_cpu_seconds is None:
            return {}

        def apply_limits():
            try:
                import resource

                if self.max_cpu_seconds is not None:
                    resource.setrlimit(resource.RLIMIT_CPU, (self.max_cpu_seconds, self.max_cpu_seconds + 1))
                if self.max_memory_mb is not None:
                    limit = self.max_memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except Exception:
                os.kill(os.getpid(), signal.SIGKILL)

        return {"preexec_fn": apply_limits}

    @staticmethod
    def _decode_output(data: bytes) -> str:
        """Decode shell output without assuming every Windows program emits UTF-8.

        Most tools and Linux processes use UTF-8. Some Windows console programs,
        notably localized ``wsl.exe`` diagnostics, emit UTF-16 instead.
        """
        if not data:
            return ""

        # ``wsl.exe`` can prepend a localized UTF-16LE diagnostic to UTF-8
        # stderr emitted by the Linux process. Decode the two segments
        # independently instead of interpreting the UTF-8 tail as CJK noise.
        for marker in (b"\r\x00\n\x00", b"\n\x00"):
            boundary = data.rfind(marker)
            if boundary < 0:
                continue
            split = boundary + len(marker)
            prefix, suffix = data[:split], data[split:]
            if not suffix:
                continue
            even = prefix[0::2]
            odd = prefix[1::2]
            if odd.count(0) / max(1, len(odd)) < 0.10:
                continue
            if prefix.startswith(b"\xff\xfe"):
                prefix = prefix[2:]
            return prefix.decode("utf-16-le", errors="replace") + suffix.decode("utf-8", errors="replace")

        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="replace")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # UTF-16 console text usually contains NUL bytes on one side of ASCII
        # code units. Keep this heuristic strict so arbitrary legacy byte output
        # is not silently reinterpreted as UTF-16.
        even = data[0::2]
        odd = data[1::2]
        even_nuls = even.count(0) / max(1, len(even))
        odd_nuls = odd.count(0) / max(1, len(odd))
        if max(even_nuls, odd_nuls) >= 0.10:
            encoding = "utf-16-le" if odd_nuls >= even_nuls else "utf-16-be"
            return data.decode(encoding, errors="replace")

        # Fall back to the active host code page for older Windows commands.
        preferred = locale.getpreferredencoding(False)
        for encoding in (preferred, "mbcs" if sys.platform == "win32" else ""):
            if not encoding or encoding.lower().replace("-", "") == "utf8":
                continue
            try:
                return data.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return data.decode("utf-8", errors="replace")

    def _decode_limited(self, stdout: bytes, stderr: bytes) -> tuple[str, str]:
        truncated = []
        if len(stdout) > self.max_output_bytes:
            stdout = stdout[:self.max_output_bytes]
            truncated.append("stdout")
        if len(stderr) > self.max_output_bytes:
            stderr = stderr[:self.max_output_bytes]
            truncated.append("stderr")

        out = self._decode_output(stdout)
        err = self._decode_output(stderr)
        if truncated:
            err = (err + "\n" if err else "") + f"[Output truncated: {', '.join(truncated)} exceeded {self.max_output_bytes} bytes]"
        return out, err

    def _preserve_output_artifact(self, stdout: bytes, stderr: bytes) -> str:
        if len(stdout) <= self.max_output_bytes and len(stderr) <= self.max_output_bytes:
            return ""
        artifact_dir = Path(self.workdir) / ".astra" / "process-output"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / f"process-{uuid.uuid4().hex}.log"
        sections = []
        if stdout:
            sections.append("[Stdout]\n" + self._decode_output(stdout))
        if stderr:
            sections.append("[Stderr]\n" + self._decode_output(stderr))
        artifact.write_text("\n".join(sections), encoding="utf-8")
        return str(artifact.resolve())

    def _time_limit_result(self, exc: BaseException) -> dict:
        """Report a run stopped at the time limit with the output it had written.

        The output is bounded and preserved like that of a finished run. The
        notice stays last in ``error`` so text appended to it reads as part of it.
        """
        stdout = getattr(exc, "stdout", b"")
        stderr = getattr(exc, "stderr", b"")
        artifact_path = self._preserve_output_artifact(stdout, stderr)
        output, error = self._decode_limited(stdout, stderr)
        if error and not error.endswith("\n"):
            error += "\n"
        return {
            "output": output,
            "error": f"{error}[Timeout] Execution exceeded {self.timeout}s",
            "exit_code": -1,
            "timed_out": True,
            "artifact_path": artifact_path,
        }

    def check_host_processes(self, command: str, environment: str = "auto") -> None:
        resolved = self.resolve_shell_environment(command, environment)
        self.process_guard.check_shell(command, foreign_namespace=resolved == "wsl")

    def _check_dangerous(self, command: str, environment: str = "auto"):
        self.check_host_processes(command, environment)
        cmd_lower = re.sub(r"\s+", " ", command.lower().strip())
        for pattern in DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                raise SandboxError(f"Command blocked by safety policy (matched: '{pattern}')")
        try:
            parts = shlex.split(command, posix=True)
        except ValueError:
            parts = command.split()
        if not parts:
            return

        executable = os.path.basename(parts[0]).lower()
        args = [arg.lower() for arg in parts[1:]]
        compact_flags = "".join(arg[1:] for arg in args if arg.startswith("-"))
        targets = [arg for arg in args if not arg.startswith("-")]

        if executable == "rm" and "r" in compact_flags and "f" in compact_flags:
            if any(target in {"/", "/*"} or target.startswith("/.") for target in targets):
                raise SandboxError("Command blocked by safety policy (recursive force delete at filesystem root)")
        if executable in {"chmod", "chown"} and any(target == "/" or target.startswith("/*") for target in targets):
            raise SandboxError("Command blocked by safety policy (root permission change)")

    async def _collect_stream(
        self,
        reader: asyncio.StreamReader | None,
        stream: str,
        on_output: OutputCallback | None,
        chunks: list[bytes] | None = None,
    ) -> bytes:
        """Read one pipe to its end. A caller-owned ``chunks`` keeps what was read if this is cancelled."""
        if reader is None:
            return b""
        if chunks is None:
            chunks = []
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

    async def _communicate_streaming(
        self,
        proc: asyncio.subprocess.Process,
        *,
        input_data: bytes | None = None,
        on_output: OutputCallback | None = None,
    ) -> tuple[bytes, bytes]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_task = asyncio.create_task(
            self._collect_stream(proc.stdout, "stdout", on_output, stdout_chunks)
        )
        stderr_task = asyncio.create_task(
            self._collect_stream(proc.stderr, "stderr", on_output, stderr_chunks)
        )
        try:
            if input_data is not None and proc.stdin is not None:
                proc.stdin.write(input_data)
                await proc.stdin.drain()
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (AttributeError, BrokenPipeError, ConnectionResetError):
                    pass
            if self.timeout is None:
                await proc.wait()
            else:
                await asyncio.wait_for(proc.wait(), timeout=self.timeout)
            return await asyncio.gather(stdout_task, stderr_task)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
            await self._settle_stopped(proc, stdout_task, stderr_task)
            raise _TimeLimitExceeded(b"".join(stdout_chunks), b"".join(stderr_chunks)) from None
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

    @staticmethod
    async def _settle_stopped(
        proc: asyncio.subprocess.Process,
        *collectors: asyncio.Task,
    ) -> None:
        """Wait for a killed process and its output, but not for a descendant.

        ``proc.wait()`` and the collectors finish only when the pipes close. A
        killed process closes them at once unless a descendant still holds
        them. That descendant is left running, as it always was; its pipes stay
        drained so it never blocks on a full one, but nothing more is kept or
        forwarded.
        """
        waiting = [asyncio.ensure_future(proc.wait()), *collectors]
        try:
            await asyncio.wait(waiting, timeout=_STOPPED_OUTPUT_WAIT_SECONDS)
        finally:
            unfinished = [task for task in waiting if not task.done()]
            for task in unfinished:
                task.cancel()
        if not unfinished:
            return
        await asyncio.gather(*unfinished, return_exceptions=True)
        for reader in (proc.stdout, proc.stderr):
            if reader is not None and not reader.at_eof():
                task = asyncio.create_task(_discard_stream(reader))
                _DISCARDING.add(task)
                task.add_done_callback(_DISCARDING.discard)

    @staticmethod
    def resolve_shell_environment(command: str, environment: str = "auto") -> str:
        requested = environment.strip().lower()
        if requested not in SHELL_ENVIRONMENTS:
            raise ValueError("Shell environment must be one of: auto, windows, wsl, posix")
        if sys.platform != "win32":
            if requested in {"windows", "wsl"}:
                raise ValueError(f"Shell environment '{requested}' is unavailable on a POSIX host")
            return "posix"
        if requested == "posix":
            raise ValueError("Shell environment 'posix' is unavailable on Windows")
        if requested != "auto":
            return requested

        stripped = command.lstrip()
        first = re.match(r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*([^\s|;&]+)", stripped)
        executable = os.path.basename(first.group(1)).lower() if first else ""
        linux_path = bool(re.search(r"(?<![A-Za-z0-9_])(?:/home/|/mnt/|/tmp/|~/|/proc/|/etc/)", command))
        wsl_unc = "\\\\wsl.localhost\\" in command.lower() or "\\\\wsl$\\" in command.lower()
        linux_syntax = "/dev/null" in command or executable in WSL_COMMANDS
        return "wsl" if linux_path or wsl_unc or linux_syntax else "windows"

    @staticmethod
    def _wsl_args(command: str) -> list[str]:
        import base64

        args = ["wsl.exe"]
        distro = os.getenv("AGENT_WSL_DISTRO", "").strip()
        if distro:
            args.extend(["-d", distro])
        # Pass the command as base64 to avoid Windows argv mangling of
        # newlines/quotes/$ (heredocs, multi-line scripts, variables).
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        args.extend(["--", "bash", "-lc", f"set -o pipefail; echo {encoded} | base64 -d | bash -o pipefail"])
        return args

    @staticmethod
    def _persistent_bash_args(platform: str | None = None) -> list[str]:
        """Build the host-appropriate PTY command line for persistent Bash."""
        target = platform or sys.platform
        if target == "win32":
            args = ["wsl.exe"]
            distro = os.getenv("AGENT_WSL_DISTRO", "").strip()
            if distro:
                args.extend(["-d", distro])
            return args + [
                "--",
                "script",
                "-qfec",
                "bash --noprofile --norc -i",
                "/dev/null",
            ]
        if target == "darwin":
            return ["script", "-q", "/dev/null", "/bin/bash", "--noprofile", "--norc", "-i"]
        return ["script", "-qfec", "bash --noprofile --norc -i", "/dev/null"]

    @staticmethod
    def _persistent_wsl_args() -> list[str]:
        """Compatibility alias for the Windows WSL persistent-Bash command."""
        return LocalSandbox._persistent_bash_args("win32")

    async def execute_python(self, code: str) -> dict:
        return await self.execute_python_stream(code)

    async def execute_python_stream(
        self,
        code: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        self.process_guard.check_python(code)
        proc = None
        job = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                **self._limit_kwargs(),
            )
            job = await self._attach_windows_job(proc)
            stdout, stderr = await self._communicate_streaming(
                proc,
                input_data=code.encode("utf-8"),
                on_output=on_output,
            )
            artifact_path = self._preserve_output_artifact(stdout, stderr)
            output, error = self._decode_limited(stdout, stderr)
            return {
                "output": output,
                "error": error,
                "exit_code": proc.returncode or 0,
                "artifact_path": artifact_path,
            }
        except asyncio.TimeoutError as exc:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return self._time_limit_result(exc)
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        finally:
            if job is not None:
                job.close()

    async def execute_shell(self, command: str, environment: str = "auto") -> dict:
        return await self.execute_shell_stream(command, environment=environment)

    async def execute_shell_stream(
        self,
        command: str,
        environment: str = "auto",
        on_output: OutputCallback | None = None,
    ) -> dict:
        self._check_dangerous(command, environment)
        resolved_environment = self.resolve_shell_environment(command, environment)
        if self.windows_job_containment and sys.platform == "win32" and resolved_environment == "wsl":
            raise SandboxError(
                "Windows Job containment does not isolate Linux processes inside WSL; "
                "use environment='windows' or DockerSandbox."
            )
        proc = None
        job = None
        try:
            if resolved_environment == "wsl" and sys.platform == "win32":
                proc = await asyncio.create_subprocess_exec(
                    *self._wsl_args(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workdir,
                    **self._limit_kwargs(),
                )
            elif resolved_environment == "posix":
                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash", "-o", "pipefail", "-lc", command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workdir,
                    **self._limit_kwargs(),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.workdir,
                    **self._limit_kwargs(),
                )
            job = await self._attach_windows_job(proc)
            stdout, stderr = await self._communicate_streaming(proc, on_output=on_output)
            artifact_path = self._preserve_output_artifact(stdout, stderr)
            output, error = self._decode_limited(stdout, stderr)
            return {
                "output": output,
                "error": error,
                "exit_code": proc.returncode or 0,
                "environment": resolved_environment,
                "artifact_path": artifact_path,
            }
        except asyncio.TimeoutError as exc:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return {**self._time_limit_result(exc), "environment": resolved_environment}
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        finally:
            if job is not None:
                job.close()

    async def execute_persistent_bash_stream(
        self,
        command: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        """Run a command in the owner-scoped persistent Bash PTY used by Minimal Mode."""
        self._check_dangerous(command, "wsl" if sys.platform == "win32" else "posix")
        if self._persistent_bash_shell is None:
            self._persistent_bash_shell = PersistentWslShell(
                self._persistent_bash_args(),
                workdir=self.workdir,
                timeout=self.persistent_bash_timeout,
                max_output_bytes=self.max_output_bytes,
                environment="wsl" if sys.platform == "win32" else "posix",
            )
        return await self._persistent_bash_shell.execute(command, on_output=on_output)

    async def execute_wsl_persistent_shell_stream(
        self,
        command: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        """Compatibility alias for :meth:`execute_persistent_bash_stream`."""
        return await self.execute_persistent_bash_stream(command, on_output=on_output)

    async def execute_persistent_bash(self, command: str) -> dict:
        return await self.execute_persistent_bash_stream(command)

    def close_persistent_bash(self) -> None:
        if self._persistent_bash_shell is not None:
            self._persistent_bash_shell.close_nowait()

    def close_wsl_shell(self) -> None:
        """Compatibility alias for :meth:`close_persistent_bash`."""
        self.close_persistent_bash()

    async def close(self) -> None:
        if self._persistent_bash_shell is not None:
            await self._persistent_bash_shell.close()
