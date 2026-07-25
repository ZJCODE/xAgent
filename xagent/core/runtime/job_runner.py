"""Per-attempt runner process for local background jobs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .job_process import (
    boot_session_id,
    process_start_identity,
    safe_basic_environment,
    signal_verified_process_group,
    wait_for_process_start_identity,
)
from .job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    JobSettings,
    JobStore,
    now_ms,
)


class RotatingBinaryLog:
    """Append-only segmented log writer with a fixed retention bound."""

    def __init__(
        self,
        directory: Path,
        stream_name: str,
        *,
        segment_bytes: int,
        segments: int,
    ) -> None:
        self.directory = directory
        self.stream_name = stream_name
        self.segment_bytes = max(1024, int(segment_bytes))
        self.segments = max(1, int(segments))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index = 0
        self.size = 0
        self.handle = self._open_segment()

    def _path(self, index: int) -> Path:
        return self.directory / f"{self.stream_name}.{index:06d}.log"

    def _open_segment(self):
        path = self._path(self.index)
        return path.open("ab", buffering=0)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            capacity = self.segment_bytes - self.size
            if capacity <= 0:
                self._rotate()
                capacity = self.segment_bytes
            chunk = view[:capacity]
            self.handle.write(chunk)
            self.size += len(chunk)
            view = view[len(chunk) :]

    def _rotate(self) -> None:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.index += 1
        self.size = 0
        self.handle = self._open_segment()
        cutoff = self.index - self.segments
        if cutoff >= 0:
            try:
                self._path(cutoff).unlink()
            except FileNotFoundError:
                pass

    def close(self) -> None:
        if self.handle.closed:
            return
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


async def _drain_stream(
    stream: Optional[asyncio.StreamReader],
    writer: RotatingBinaryLog,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return
        writer.write(chunk)


async def _terminate_child(
    process: asyncio.subprocess.Process,
    *,
    child_boot_id: str,
    child_start_identity: Optional[str],
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    signal_verified_process_group(
        process.pid,
        expected_boot_id=child_boot_id,
        expected_start_identity=child_start_identity or "",
        sig=signal.SIGTERM,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.1, grace_seconds))
        return
    except asyncio.TimeoutError:
        pass
    signal_verified_process_group(
        process.pid,
        expected_boot_id=child_boot_id,
        expected_start_identity=child_start_identity or "",
        sig=signal.SIGKILL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        # The leader may have disappeared between identity verification and
        # wait(). A final direct kill is safe because it is our own child.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def run_attempt(
    *,
    jobs_dir: Path,
    workspace_dir: Path,
    job_id: str,
    attempt_id: str,
    runner_token: str,
    settings: JobSettings,
) -> int:
    store = JobStore(
        jobs_dir,
        workspace_dir=workspace_dir,
        settings=settings,
    )
    record = store.get_job(job_id)
    if not record.attempt or str(record.attempt.get("id")) != attempt_id:
        raise RuntimeError("attempt is no longer current")
    if str(record.attempt.get("runner_token")) != runner_token:
        raise RuntimeError("attempt token mismatch")

    attempt_dir = jobs_dir / job_id / attempt_id
    receipt_path = attempt_dir / "result.json"
    if store.runner_should_cancel(attempt_id, runner_token=runner_token):
        payload = {
            "state": JOB_STATUS_CANCELLED,
            "reason": "user_cancelled",
            "result": {"summary": "Cancelled before process start"},
            "exit_code": None,
            "exit_signal": None,
            "last_error": None,
            "ended_at_ms": now_ms(),
        }
        _write_receipt(receipt_path, payload)
        store.finish_attempt(
            attempt_id,
            runner_token=runner_token,
            receipt_path=str(receipt_path),
            **payload,
        )
        return 0

    stdout_writer = RotatingBinaryLog(
        attempt_dir,
        "stdout",
        segment_bytes=settings.log_segment_bytes,
        segments=settings.log_segments,
    )
    stderr_writer = RotatingBinaryLog(
        attempt_dir,
        "stderr",
        segment_bytes=settings.log_segment_bytes,
        segments=settings.log_segments,
    )
    process: Optional[asyncio.subprocess.Process] = None
    runner_boot = boot_session_id()
    runner_start = process_start_identity(os.getpid())
    try:
        spec = record.spec
        env = safe_basic_environment()
        common = {
            "cwd": str(spec["cwd"]),
            "env": env,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "start_new_session": True,
        }
        if bool(spec.get("shell")):
            process = await asyncio.create_subprocess_shell(
                str(spec.get("command") or ""),
                executable="/bin/sh",
                **common,
            )
        else:
            argv = [str(item) for item in (spec.get("argv") or [])]
            process = await asyncio.create_subprocess_exec(*argv, **common)

        child_boot = boot_session_id()
        child_start = wait_for_process_start_identity(process.pid)
        record = store.runner_begin(
            attempt_id,
            runner_token=runner_token,
            runner_pid=os.getpid(),
            runner_boot_id=runner_boot,
            runner_start_identity=runner_start,
            child_pid=process.pid,
            child_pgid=process.pid,
            child_boot_id=child_boot,
            child_start_identity=child_start,
        )
        stdout_task = asyncio.create_task(_drain_stream(process.stdout, stdout_writer))
        stderr_task = asyncio.create_task(_drain_stream(process.stderr, stderr_writer))

        deadline_ms = record.data.get("deadline_at_ms")
        last_heartbeat = 0.0
        cancelled = False
        timed_out = False
        while process.returncode is None:
            monotonic_now = time.monotonic()
            if monotonic_now - last_heartbeat >= settings.runner_heartbeat_seconds:
                try:
                    store.heartbeat_attempt(attempt_id, runner_token=runner_token)
                except (sqlite3.Error, RuntimeError):  # type: ignore[name-defined]
                    # The receipt remains the recovery authority if SQLite is
                    # temporarily unavailable.
                    pass
                last_heartbeat = monotonic_now
            if store.runner_should_cancel(attempt_id, runner_token=runner_token):
                cancelled = True
                await _terminate_child(
                    process,
                    child_boot_id=child_boot,
                    child_start_identity=child_start,
                    grace_seconds=settings.cancel_grace_seconds,
                )
                break
            if deadline_ms is not None and now_ms() >= int(deadline_ms):
                timed_out = True
                await _terminate_child(
                    process,
                    child_boot_id=child_boot,
                    child_start_identity=child_start,
                    grace_seconds=settings.cancel_grace_seconds,
                )
                break
            try:
                await asyncio.wait_for(process.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

        return_code = await process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        exit_signal = -return_code if return_code < 0 else None
        ended_at = now_ms()
        if cancelled:
            state = JOB_STATUS_CANCELLED
            reason = "user_cancelled"
            summary = "Cancelled"
            last_error = None
        elif timed_out:
            state = JOB_STATUS_FAILED
            reason = "timeout"
            summary = "Failed because the configured timeout was reached"
            last_error = summary
        elif return_code == 0:
            state = JOB_STATUS_SUCCEEDED
            reason = "exit_zero"
            summary = "Completed successfully"
            last_error = None
        else:
            state = JOB_STATUS_FAILED
            reason = "nonzero_exit"
            summary = f"Failed with exit code {return_code}"
            last_error = summary

        payload = {
            "state": state,
            "reason": reason,
            "result": {
                "summary": summary,
                "exit_code": return_code,
                "stdout_log_dir": str(attempt_dir),
                "stderr_log_dir": str(attempt_dir),
            },
            "exit_code": return_code,
            "exit_signal": exit_signal,
            "last_error": last_error,
            "ended_at_ms": ended_at,
        }
        _write_receipt(receipt_path, payload)
        for retry in range(20):
            try:
                store.finish_attempt(
                    attempt_id,
                    runner_token=runner_token,
                    receipt_path=str(receipt_path),
                    **payload,
                )
                return 0
            except Exception:
                if retry == 19:
                    return 2
                await asyncio.sleep(0.25)
        return 2
    except Exception as exc:
        if process is not None and process.returncode is None:
            child_start = process_start_identity(process.pid)
            if child_start:
                await _terminate_child(
                    process,
                    child_boot_id=boot_session_id(),
                    child_start_identity=child_start,
                    grace_seconds=settings.cancel_grace_seconds,
                )
        summary = f"Failed to start or supervise process: {exc}"
        payload = {
            "state": JOB_STATUS_FAILED,
            "reason": "runner_error",
            "result": {"summary": summary},
            "exit_code": process.returncode if process is not None else None,
            "exit_signal": None,
            "last_error": summary[:500],
            "ended_at_ms": now_ms(),
        }
        _write_receipt(receipt_path, payload)
        try:
            store.finish_attempt(
                attempt_id,
                runner_token=runner_token,
                receipt_path=str(receipt_path),
                **payload,
            )
        except Exception:
            pass
        return 1
    finally:
        stdout_writer.close()
        stderr_writer.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal xAgent job attempt runner")
    parser.add_argument("--jobs-dir", required=True)
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--runner-token", required=True)
    parser.add_argument("--settings-json", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = JobSettings.from_mapping(json.loads(args.settings_json))
    return asyncio.run(
        run_attempt(
            jobs_dir=Path(args.jobs_dir).expanduser().resolve(),
            workspace_dir=Path(args.workspace_dir).expanduser().resolve(),
            job_id=args.job_id,
            attempt_id=args.attempt_id,
            runner_token=args.runner_token,
            settings=settings,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
