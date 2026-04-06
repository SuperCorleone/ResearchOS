"""Execution backends for running experiment code.

Provides local (subprocess), Docker, and SSH backends behind a common
interface so that ``agent_executor`` can delegate code execution without
caring where or how the code actually runs.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
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
# Factory
# ---------------------------------------------------------------------------


def create_backend(config: dict) -> ExecutionBackend:
    """Instantiate an execution backend from a configuration dict.

    Expected structure::

        {
            "execution": {
                "backend": "local" | "docker" | "ssh",
                "docker": {"image": "...", "gpu": false, "memory_limit": "8g"},
                "ssh": {"host": "...", "user": "...", "key_path": "...", "remote_dir": "..."}
            }
        }

    Falls back to :class:`LocalBackend` when the backend key is missing or
    unrecognised.
    """
    exec_cfg = config.get("execution", {})
    backend_name = exec_cfg.get("backend", "local").lower()

    if backend_name == "local":
        return LocalBackend()

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
