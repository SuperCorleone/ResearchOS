"""Execution backends for running experiment code.

Provides local (subprocess), Docker, SSH, and sandbox backends behind a
common interface so that ``agent_executor`` can delegate code execution
without caring where or how the code actually runs.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Truncation constants – keep in sync with agent_executor._execute_python
# ---------------------------------------------------------------------------
_STDOUT_TAIL = 4000
_STDERR_TAIL = 2000


def _format_output(
    stdout: str,
    stderr: str,
    returncode: int | None,
) -> str:
    """Build the combined output string with consistent truncation."""
    parts: list[str] = []
    if stdout:
        parts.append(f"STDOUT:\n{stdout[-_STDOUT_TAIL:]}")
    if stderr:
        parts.append(f"STDERR:\n{stderr[-_STDERR_TAIL:]}")
    if returncode is not None:
        parts.append(f"EXIT CODE: {returncode}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ExecutionBackend(ABC):
    """Interface every execution backend must implement."""

    @abstractmethod
    def execute(
        self,
        code: str,
        timeout: int,
        working_dir: Path,
        data_dir: Path | None = None,
    ) -> str:
        """Execute *code* and return a stdout+stderr summary string.

        Parameters
        ----------
        code:
            Python source code to run.
        timeout:
            Maximum wall-clock seconds before the run is killed.
        working_dir:
            Host directory used as the working/scratch directory.
        data_dir:
            Optional host directory containing datasets to mount or
            make available to the script.
        """


# ---------------------------------------------------------------------------
# Local backend – mirrors agent_executor._execute_python
# ---------------------------------------------------------------------------


class LocalBackend(ExecutionBackend):
    """Run code as a local subprocess (the current default)."""

    def execute(
        self,
        code: str,
        timeout: int,
        working_dir: Path,
        data_dir: Path | None = None,
    ) -> str:
        working_dir.mkdir(parents=True, exist_ok=True)

        fd, script_path = tempfile.mkstemp(
            suffix=".py", dir=str(working_dir), prefix="ros_"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(code)

            env = {**os.environ, "PYTHONPATH": str(working_dir)}
            if data_dir is not None:
                env["DATA_DIR"] = str(data_dir)

            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(working_dir),
                env=env,
            )
            return _format_output(result.stdout, result.stderr, result.returncode)
        except subprocess.TimeoutExpired:
            return f"ERROR: Script timed out after {timeout} seconds."
        except Exception as exc:
            return f"ERROR: {exc}"
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------


class DockerBackend(ExecutionBackend):
    """Run code inside a Docker container.

    Parameters
    ----------
    image:
        Docker image name (e.g. ``"python:3.12-slim"``).
    gpu:
        If ``True``, pass ``--gpus all`` to ``docker run``.
    memory_limit:
        Docker memory limit string, e.g. ``"8g"``.
    """

    def __init__(
        self,
        image: str = "python:3.12-slim",
        gpu: bool = False,
        memory_limit: str | None = None,
    ) -> None:
        if shutil.which("docker") is None:
            raise EnvironmentError(
                "DockerBackend requires the 'docker' CLI to be installed and on PATH."
            )
        self.image = image
        self.gpu = gpu
        self.memory_limit = memory_limit

    def execute(
        self,
        code: str,
        timeout: int,
        working_dir: Path,
        data_dir: Path | None = None,
    ) -> str:
        working_dir.mkdir(parents=True, exist_ok=True)

        script_name = "ros_script.py"
        script_host_path = working_dir / script_name
        script_host_path.write_text(code, encoding="utf-8")

        try:
            cmd: list[str] = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{working_dir.resolve()}:/workspace",
            ]

            if data_dir is not None:
                cmd += ["-v", f"{data_dir.resolve()}:/data"]

            if self.gpu:
                cmd += ["--gpus", "all"]

            if self.memory_limit:
                cmd += ["-m", self.memory_limit]

            cmd += [self.image, "python", f"/workspace/{script_name}"]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return _format_output(result.stdout, result.stderr, result.returncode)
        except subprocess.TimeoutExpired:
            return f"ERROR: Script timed out after {timeout} seconds."
        except Exception as exc:
            return f"ERROR: {exc}"
        finally:
            try:
                script_host_path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# SSH backend
# ---------------------------------------------------------------------------


class SSHBackend(ExecutionBackend):
    """Run code on a remote host over SSH.

    Parameters
    ----------
    host:
        SSH hostname or IP address.
    user:
        SSH user name.
    key_path:
        Path to the private key file.
    remote_dir:
        Directory on the remote host used for script staging.
    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: str | Path,
        remote_dir: str = "/tmp/researchos",
    ) -> None:
        if shutil.which("ssh") is None:
            raise EnvironmentError(
                "SSHBackend requires the 'ssh' CLI to be installed and on PATH."
            )
        if shutil.which("scp") is None:
            raise EnvironmentError(
                "SSHBackend requires the 'scp' CLI to be installed and on PATH."
            )
        self.host = host
        self.user = user
        self.key_path = Path(key_path)
        self.remote_dir = remote_dir

        if not self.key_path.exists():
            raise FileNotFoundError(
                f"SSH key not found: {self.key_path}"
            )

    def _ssh_target(self) -> str:
        return f"{self.user}@{self.host}"

    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.key_path),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
            self._ssh_target(),
        ]

    def _scp_base(self) -> list[str]:
        return [
            "scp",
            "-i",
            str(self.key_path),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
        ]

    def execute(
        self,
        code: str,
        timeout: int,
        working_dir: Path,
        data_dir: Path | None = None,
    ) -> str:
        working_dir.mkdir(parents=True, exist_ok=True)

        local_script = working_dir / "ros_script.py"
        local_script.write_text(code, encoding="utf-8")
        remote_script = f"{self.remote_dir}/ros_script.py"

        try:
            # Ensure remote directory exists
            subprocess.run(
                self._ssh_base() + [f"mkdir -p {shlex.quote(self.remote_dir)}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )

            # Upload the script
            subprocess.run(
                self._scp_base()
                + [str(local_script), f"{self._ssh_target()}:{remote_script}"],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )

            # Execute remotely
            result = subprocess.run(
                self._ssh_base()
                + [f"cd {shlex.quote(self.remote_dir)} && python3 {shlex.quote(remote_script)}"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return _format_output(result.stdout, result.stderr, result.returncode)
        except subprocess.TimeoutExpired:
            return f"ERROR: Script timed out after {timeout} seconds."
        except subprocess.CalledProcessError as exc:
            return f"ERROR: SSH/SCP command failed (exit {exc.returncode}): {exc.stderr}"
        except Exception as exc:
            return f"ERROR: {exc}"
        finally:
            # Cleanup local temp
            try:
                local_script.unlink(missing_ok=True)
            except OSError:
                pass
            # Cleanup remote script (best-effort)
            try:
                subprocess.run(
                    self._ssh_base() + [f"rm -f {shlex.quote(remote_script)}"],
                    capture_output=True,
                    timeout=15,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bash Exec Sandbox (A2 L1)
# ---------------------------------------------------------------------------


def _default_allowed_roots(project_root: Path | None = None) -> list[Path]:
    """Return default allowed cwd roots for the sandbox.

    Defaults are project_root/artifacts, project_root/data, project_root/scratch.
    When ``project_root`` is None, uses the current working directory.
    """
    base = Path(project_root) if project_root is not None else Path.cwd()
    return [base / "artifacts", base / "data", base / "scratch"]


def _cwd_within_allowed(cwd: Path, allowed_roots: list[Path]) -> bool:
    """Return True if *cwd* is contained in any of *allowed_roots*."""
    try:
        cwd_resolved = cwd.resolve()
    except Exception:
        return False
    for root in allowed_roots:
        try:
            root_resolved = root.resolve()
        except Exception:
            continue
        try:
            cwd_resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


class BashExecSandbox:
    """Sandboxed bash executor with watchdog + allowed-roots enforcement.

    Inspired by DeepScientist OSS ``src/deepscientist/bash_exec/runtime.py``.

    Parameters
    ----------
    project_root:
        Project root used to compute default allowed roots.
    allowed_roots:
        Optional explicit list of roots within which ``cwd`` must live.
        Defaults to ``artifacts``, ``data``, ``scratch`` under project_root.
    log_path:
        Path to append JSONL execution records to. Defaults to
        ``{project_root}/runtime/bash_exec.log``.
    """

    def __init__(
        self,
        project_root: Path | None = None,
        allowed_roots: list[Path] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.allowed_roots = (
            [Path(p) for p in allowed_roots]
            if allowed_roots is not None
            else _default_allowed_roots(self.project_root)
        )
        self.log_path = (
            Path(log_path)
            if log_path is not None
            else self.project_root / "runtime" / "bash_exec.log"
        )

    # -- JSONL log -----------------------------------------------------------

    def _log(self, entry: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.warning("Failed to write bash_exec log: %s", exc)

    # -- Core run ------------------------------------------------------------

    def run(
        self,
        cmd: str,
        cwd: Path,
        timeout: int = 1800,
        cuda_device: str | None = None,
    ) -> dict:
        """Execute *cmd* in *cwd* under watchdog + cwd sandboxing.

        Returns a dict with ``returncode``, ``stdout``, ``stderr``,
        ``timed_out``, ``duration_s``, and ``cwd``.
        """
        cwd = Path(cwd)
        started_at = datetime.now(timezone.utc).isoformat()
        start = time.time()

        if not _cwd_within_allowed(cwd, self.allowed_roots):
            result = {
                "returncode": None,
                "stdout": "",
                "stderr": (
                    f"ERROR: cwd {cwd} is not within allowed_roots "
                    f"{[str(p) for p in self.allowed_roots]}"
                ),
                "timed_out": False,
                "duration_s": 0.0,
                "cwd": str(cwd),
            }
            self._log({
                "ts": started_at,
                "cmd": cmd,
                "cwd": str(cwd),
                "timeout": timeout,
                "cuda_device": cuda_device,
                "rejected": True,
                "reason": "cwd_not_allowed",
            })
            return result

        cwd.mkdir(parents=True, exist_ok=True)

        env = {**os.environ}
        if cuda_device is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(cuda_device)

        use_pty = os.name == "posix"
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        timed_out = False
        returncode: int | None = None

        try:
            if use_pty:
                # PTY path: merges stdout+stderr; use a single master fd
                import pty

                try:
                    master_fd, slave_fd = pty.openpty()
                except OSError as exc:
                    # PTY devices exhausted (common in CI/sandbox) — fall
                    # back to the pipe path below.
                    logger.debug("PTY unavailable (%s); using pipe fallback", exc)
                    use_pty = False
                    master_fd = slave_fd = -1
            if use_pty:
                try:
                    proc = subprocess.Popen(
                        cmd,
                        shell=True,
                        cwd=str(cwd),
                        env=env,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        close_fds=True,
                    )
                    os.close(slave_fd)

                    def _watchdog() -> None:
                        nonlocal timed_out
                        try:
                            proc.wait(timeout=timeout)
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            try:
                                proc.kill()
                            except Exception:
                                pass

                    watchdog = threading.Thread(target=_watchdog, daemon=True)
                    watchdog.start()

                    sel = selectors.DefaultSelector()
                    sel.register(master_fd, selectors.EVENT_READ)
                    while True:
                        if proc.poll() is not None and not _fd_readable(
                            sel, timeout=0.05
                        ):
                            break
                        events = sel.select(timeout=0.1)
                        if not events:
                            continue
                        try:
                            chunk = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        stdout_buf.append(chunk.decode(errors="replace"))

                    watchdog.join(timeout=5)
                    returncode = proc.returncode
                finally:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
            else:
                # Pipe fallback (non-POSIX, or POSIX with no PTY available)
                try:
                    completed = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=str(cwd),
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    stdout_buf.append(completed.stdout or "")
                    stderr_buf.append(completed.stderr or "")
                    returncode = completed.returncode
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    stdout_buf.append(exc.stdout or "" if exc.stdout else "")
                    stderr_buf.append(exc.stderr or "" if exc.stderr else "")
        except Exception as exc:
            stderr_buf.append(f"ERROR: {exc}")

        duration = time.time() - start
        result = {
            "returncode": returncode,
            "stdout": "".join(stdout_buf),
            "stderr": "".join(stderr_buf),
            "timed_out": timed_out,
            "duration_s": round(duration, 3),
            "cwd": str(cwd),
        }

        self._log({
            "ts": started_at,
            "cmd": cmd,
            "cwd": str(cwd),
            "timeout": timeout,
            "cuda_device": cuda_device,
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_s": result["duration_s"],
            "stdout_len": len(result["stdout"]),
            "stderr_len": len(result["stderr"]),
        })
        return result


def _fd_readable(sel: "selectors.DefaultSelector", timeout: float) -> bool:
    """Tiny helper: True if any registered fd has data to read in *timeout* s."""
    events = sel.select(timeout=timeout)
    return bool(events)


class SandboxBackend(ExecutionBackend):
    """ExecutionBackend adapter that writes *code* then runs it via BashExecSandbox."""

    def __init__(
        self,
        project_root: Path | None = None,
        allowed_roots: list[Path] | None = None,
        python_bin: str | None = None,
    ) -> None:
        self.sandbox = BashExecSandbox(
            project_root=project_root, allowed_roots=allowed_roots
        )
        self.python_bin = python_bin or sys.executable

    def execute(
        self,
        code: str,
        timeout: int,
        working_dir: Path,
        data_dir: Path | None = None,
    ) -> str:
        working_dir.mkdir(parents=True, exist_ok=True)
        script_path = working_dir / "ros_sandbox_script.py"
        script_path.write_text(code, encoding="utf-8")
        try:
            cuda_device = os.environ.get("CUDA_VISIBLE_DEVICES")
            result = self.sandbox.run(
                cmd=f"{shlex.quote(self.python_bin)} {shlex.quote(str(script_path))}",
                cwd=working_dir,
                timeout=timeout,
                cuda_device=cuda_device,
            )
            if result.get("timed_out"):
                return f"ERROR: Script timed out after {timeout} seconds."
            return _format_output(
                result.get("stdout", ""),
                result.get("stderr", ""),
                result.get("returncode"),
            )
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def execute_backend(backend_name: str, /, **kwargs):
    """Dispatch a single execution against a backend by name.

    Currently supports ``"sandbox"``, which runs ``BashExecSandbox.run`` with
    ``cmd``, ``cwd``, ``timeout``, ``cuda_device`` kwargs. Other backend names
    raise NotImplementedError (existing local/docker/ssh go via :func:`create_backend`).
    """
    name = (backend_name or "").lower()
    if name == "sandbox":
        project_root = kwargs.pop("project_root", None)
        allowed_roots = kwargs.pop("allowed_roots", None)
        sandbox = BashExecSandbox(
            project_root=project_root, allowed_roots=allowed_roots
        )
        cmd = kwargs.pop("cmd")
        cwd = kwargs.pop("cwd")
        timeout = kwargs.pop("timeout", 1800)
        cuda_device = kwargs.pop("cuda_device", None)
        return sandbox.run(
            cmd=cmd, cwd=cwd, timeout=timeout, cuda_device=cuda_device
        )
    raise NotImplementedError(
        f"execute_backend does not support backend_name={backend_name!r}; "
        "use create_backend() for local/docker/ssh."
    )


def create_backend(config: dict) -> ExecutionBackend:
    """Instantiate an execution backend from a configuration dict.

    Expected structure::

        {
            "execution": {
                "backend": "local" | "docker" | "ssh" | "sandbox",
                "docker": {"image": "...", "gpu": false, "memory_limit": "8g"},
                "ssh": {"host": "...", "user": "...", "key_path": "...", "remote_dir": "..."},
                "sandbox": {"project_root": "...", "allowed_roots": ["..."]}
            }
        }

    Falls back to :class:`LocalBackend` when the backend key is missing or
    unrecognised.
    """
    exec_cfg = config.get("execution", {})
    backend_name = exec_cfg.get("backend", "local").lower()

    if backend_name == "local":
        return LocalBackend()

    if backend_name == "sandbox":
        sb_cfg = exec_cfg.get("sandbox", {})
        return SandboxBackend(
            project_root=sb_cfg.get("project_root"),
            allowed_roots=sb_cfg.get("allowed_roots"),
            python_bin=sb_cfg.get("python_bin"),
        )

    if backend_name == "docker":
        docker_cfg = exec_cfg.get("docker", {})
        return DockerBackend(
            image=docker_cfg.get("image", "python:3.12-slim"),
            gpu=docker_cfg.get("gpu", False),
            memory_limit=docker_cfg.get("memory_limit"),
        )

    if backend_name == "ssh":
        ssh_cfg = exec_cfg.get("ssh", {})
        required = ("host", "user", "key_path")
        missing = [k for k in required if k not in ssh_cfg]
        if missing:
            raise ValueError(
                f"SSHBackend config missing required keys: {', '.join(missing)}"
            )
        return SSHBackend(
            host=ssh_cfg["host"],
            user=ssh_cfg["user"],
            key_path=ssh_cfg["key_path"],
            remote_dir=ssh_cfg.get("remote_dir", "/tmp/researchos"),
        )

    logger.warning(
        "Unknown execution backend %r; falling back to LocalBackend.", backend_name
    )
    return LocalBackend()
