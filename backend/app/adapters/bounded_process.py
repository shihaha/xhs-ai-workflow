"""Bounded subprocess execution with whole-tree ownership.

The Windows implementation creates the process inside a kill-on-close Job Object
at process creation time.  The POSIX fallback owns a fresh process group.  Both
implementations keep one deadline for execution, output draining and cleanup.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import BinaryIO


class BoundedProcessError(RuntimeError):
    """A persistence-safe process failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def run_bounded_process(
    argv: Sequence[str],
    *,
    shell: bool,
    cwd: Path | str,
    env: dict[str, str],
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    input_bytes: bytes = b"",
    cancel_event: Event | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one fixed argv and retain ownership of every descendant."""
    if shell:
        raise ValueError("The XHS process boundary never permits a shell.")
    if not argv or timeout <= 0 or max_stdout_bytes < 1 or max_stderr_bytes < 1:
        raise ValueError("The XHS process bounds must be positive.")
    if not isinstance(input_bytes, bytes):
        raise TypeError("Process stdin must be bytes.")
    deadline = monotonic() + timeout
    if os.name == "nt":
        return _run_windows_job(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            deadline=deadline,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            input_bytes=input_bytes,
            cancel_event=cancel_event,
        )
    return _run_posix_group(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        deadline=deadline,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        input_bytes=input_bytes,
        cancel_event=cancel_event,
    )


@dataclass
class _CaptureState:
    overflow: Event
    stop: Event
    stdout: bytearray
    stderr: bytearray
    errors: list[BaseException]
    threads: tuple[Thread, ...]
    started: list[Thread]


class _OwnedFdStream:
    """Minimal unbuffered pipe stream whose close is one bounded CRT fd close."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    def fileno(self) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise ValueError("I/O operation on closed pipe")
        return descriptor

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("Bounded pipe reads require an explicit size")
        return os.read(self.fileno(), size)

    def write(self, encoded: bytes) -> int:
        view = memoryview(encoded)
        written = 0
        while written < len(view):
            count = os.write(self.fileno(), view[written:])
            if count <= 0:
                raise OSError("Pipe write made no progress")
            written += count
        return written

    def flush(self) -> None:
        return None

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)


def _prepare_capture(
    stdout_stream: BinaryIO,
    stderr_stream: BinaryIO,
    stdin_stream: BinaryIO,
    *,
    input_bytes: bytes,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> _CaptureState:
    overflow = Event()
    stop = Event()
    stdout = bytearray()
    stderr = bytearray()
    errors: list[BaseException] = []

    def read_bounded(stream: BinaryIO, target: bytearray, limit: int) -> None:
        try:
            while not overflow.is_set() and not stop.is_set():
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = limit - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    overflow.set()
                    return
        except (OSError, ValueError) as error:
            if not stop.is_set():
                errors.append(error)

    def write_input() -> None:
        try:
            if input_bytes:
                stdin_stream.write(input_bytes)
                stdin_stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stdin_stream.close()
            except (OSError, ValueError):
                pass

    threads = (
        Thread(
            target=read_bounded,
            args=(stdout_stream, stdout, max_stdout_bytes),
            name="xhs-cli-stdout",
            daemon=True,
        ),
        Thread(
            target=read_bounded,
            args=(stderr_stream, stderr, max_stderr_bytes),
            name="xhs-cli-stderr",
            daemon=True,
        ),
        Thread(target=write_input, name="xhs-cli-stdin", daemon=True),
    )
    return _CaptureState(
        overflow=overflow,
        stop=stop,
        stdout=stdout,
        stderr=stderr,
        errors=errors,
        threads=threads,
        started=[],
    )


def _start_capture(capture: _CaptureState) -> None:
    for thread in capture.threads:
        try:
            thread.start()
        except BaseException:
            if thread.ident is not None and thread not in capture.started:
                capture.started.append(thread)
            raise
        capture.started.append(thread)


def _finish_capture(capture: _CaptureState, *, deadline: float) -> bool:
    for thread in capture.started:
        thread.join(timeout=max(deadline - monotonic(), 0))
    return all(not thread.is_alive() for thread in capture.started)


def _run_posix_group(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    env: dict[str, str],
    timeout: float,
    deadline: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    input_bytes: bytes,
    cancel_event: Event | None,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(argv),
        shell=False,
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    capture = _prepare_capture(
        process.stdout,
        process.stderr,
        process.stdin,
        input_bytes=input_bytes,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )
    _start_capture(capture)
    cleanup_reserve = min(0.05, timeout / 3)
    execution_deadline = deadline - cleanup_reserve
    category: str | None = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            category = "cancelled"
            break
        if capture.overflow.is_set():
            category = "output_too_large"
            break
        if process.poll() is not None and not _posix_group_alive(process.pid) and all(
            not thread.is_alive() for thread in capture.started
        ):
            break
        if monotonic() >= execution_deadline:
            category = "timeout"
            break
        sleep(0.005)

    if category is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    cleanup_deadline = (
        min(deadline, monotonic() + 0.15)
        if category in {"cancelled", "output_too_large"}
        else deadline
    )
    try:
        process.wait(timeout=max(cleanup_deadline - monotonic(), 0))
    except (OSError, subprocess.TimeoutExpired):
        category = "process_cleanup_failed"
    capture.stop.set()
    clean = _finish_capture(capture, deadline=cleanup_deadline)
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    if not clean or capture.errors:
        raise BoundedProcessError("process_cleanup_failed")
    if category is not None:
        raise BoundedProcessError(category)
    return subprocess.CompletedProcess(
        list(argv), int(process.returncode or 0), bytes(capture.stdout), bytes(capture.stderr)
    )


def _posix_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _run_windows_job(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    env: dict[str, str],
    timeout: float,
    deadline: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    input_bytes: bytes,
    cancel_event: Event | None,
) -> subprocess.CompletedProcess[bytes]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    invalid_handle = wintypes.HANDLE(-1).value
    handles_to_close: list[int] = []

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID, wintypes.DWORD, ctypes.c_size_t, wintypes.LPVOID,
        ctypes.c_size_t, wintypes.LPVOID, wintypes.LPVOID,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CancelSynchronousIo.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    def checked_handle(handle: int | None, operation: str) -> int:
        if handle in (None, 0, invalid_handle):
            raise OSError(ctypes.get_last_error(), operation)
        return int(handle)

    def close_handle(handle: int | None) -> None:
        if handle not in (None, 0, invalid_handle):
            kernel32.CloseHandle(wintypes.HANDLE(handle))

    def wait_for_process(handle: int | None, *, until: float) -> bool:
        if handle in (None, 0, invalid_handle):
            return True
        while True:
            remaining = until - monotonic()
            wait_ms = 0 if remaining <= 0 else min(max(int(remaining * 1000), 1), 10)
            result = kernel32.WaitForSingleObject(wintypes.HANDLE(handle), wait_ms)
            if result == 0:
                return True
            if result == 0xFFFFFFFF or remaining <= 0:
                return False

    def cancel_capture_io(capture: _CaptureState | None) -> bool:
        if capture is None:
            return True
        capture.stop.set()
        clean = True
        for reader in capture.started:
            if not reader.is_alive() or reader.native_id is None:
                continue
            thread_os_handle = kernel32.OpenThread(0x0001, False, reader.native_id)
            if thread_os_handle in (None, 0, invalid_handle):
                clean = False
                continue
            try:
                if not kernel32.CancelSynchronousIo(thread_os_handle):
                    error = ctypes.get_last_error()
                    if error != 1168:  # ERROR_NOT_FOUND means no synchronous I/O was pending.
                        clean = False
            finally:
                close_handle(int(thread_os_handle))
        return clean

    def pipe() -> tuple[int, int]:
        read_handle, write_handle = wintypes.HANDLE(), wintypes.HANDLE()
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        if not kernel32.CreatePipe(
            ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attributes), 0
        ):
            raise OSError(ctypes.get_last_error(), "CreatePipe")
        return int(read_handle.value), int(write_handle.value)

    stdout_read = stdout_write = stderr_read = stderr_write = stdin_read = stdin_write = None
    job_handle = process_handle = thread_handle = None
    attribute_buffer = None
    attribute_list_initialized = False
    fds_to_close: list[int] = []
    streams_to_close: list[BinaryIO] = []
    capture: _CaptureState | None = None

    def terminate_and_close_job() -> bool:
        """Stop the whole tree before any pipe or process handle is released."""
        nonlocal job_handle
        if job_handle in (None, 0, invalid_handle):
            return True
        terminated = False
        closed = False
        try:
            terminated = bool(
                kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1)
            )
        except (OSError, ValueError):
            terminated = False
        finally:
            try:
                closed = bool(kernel32.CloseHandle(wintypes.HANDLE(job_handle)))
            except (OSError, ValueError):
                closed = False
            job_handle = None
        return terminated and closed

    def cleanup_after_created_process(*, until: float) -> bool:
        job_clean = terminate_and_close_job()
        io_cancelled = cancel_capture_io(capture)
        process_clean = wait_for_process(process_handle, until=until)
        readers_clean = capture is None or _finish_capture(capture, deadline=until)
        return job_clean and io_cancelled and process_clean and readers_clean

    try:
        stdout_read, stdout_write = pipe()
        stderr_read, stderr_write = pipe()
        stdin_read, stdin_write = pipe()
        handles_to_close.extend([
            stdout_read, stdout_write, stderr_read, stderr_write, stdin_read, stdin_write
        ])
        for parent_handle in (stdout_read, stderr_read, stdin_write):
            if not kernel32.SetHandleInformation(wintypes.HANDLE(parent_handle), 1, 0):
                raise OSError(ctypes.get_last_error(), "SetHandleInformation")

        job_handle = checked_handle(kernel32.CreateJobObjectW(None, None), "CreateJobObjectW")
        extended = EXTENDED_LIMIT()
        extended.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(job_handle), 9, ctypes.byref(extended), ctypes.sizeof(extended)
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")

        attribute_size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(attribute_size))
        attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
        attribute_pointer = ctypes.cast(attribute_buffer, wintypes.LPVOID)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_pointer, 2, 0, ctypes.byref(attribute_size)
        ):
            raise OSError(ctypes.get_last_error(), "InitializeProcThreadAttributeList")
        attribute_list_initialized = True
        inherited = (wintypes.HANDLE * 3)(stdin_read, stdout_write, stderr_write)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_pointer,
            0,
            0x00020002,
            ctypes.cast(inherited, wintypes.LPVOID),
            ctypes.sizeof(inherited),
            None,
            None,
        ):
            raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute(handle_list)")
        jobs = (wintypes.HANDLE * 1)(job_handle)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_pointer,
            0,
            0x0002000D,
            ctypes.cast(jobs, wintypes.LPVOID),
            ctypes.sizeof(jobs),
            None,
            None,
        ):
            raise OSError(ctypes.get_last_error(), "UpdateProcThreadAttribute(job_list)")

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = 0x00000100
        startup.StartupInfo.hStdInput = wintypes.HANDLE(stdin_read)
        startup.StartupInfo.hStdOutput = wintypes.HANDLE(stdout_write)
        startup.StartupInfo.hStdError = wintypes.HANDLE(stderr_write)
        startup.lpAttributeList = attribute_pointer
        process_info = PROCESS_INFORMATION()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
        environment_text = "\0".join(
            f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].casefold())
        ) + "\0\0"
        environment_block = ctypes.create_unicode_buffer(environment_text)
        created = kernel32.CreateProcessW(
            str(argv[0]),
            command_line,
            None,
            None,
            True,
            0x00080000 | 0x00000400 | 0x08000000,
            ctypes.cast(environment_block, wintypes.LPVOID),
            str(cwd),
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_info),
        )
        if not created:
            raise OSError(ctypes.get_last_error(), "CreateProcessW")
        process_handle = int(process_info.hProcess)
        thread_handle = int(process_info.hThread)
        close_handle(thread_handle)
        thread_handle = None
        for child_handle_name in ("stdout_write", "stderr_write", "stdin_read"):
            value = locals()[child_handle_name]
            close_handle(value)
            handles_to_close.remove(value)
            if child_handle_name == "stdout_write":
                stdout_write = None
            elif child_handle_name == "stderr_write":
                stderr_write = None
            else:
                stdin_read = None

        stdout_fd = msvcrt.open_osfhandle(stdout_read, os.O_RDONLY | os.O_BINARY)
        handles_to_close.remove(stdout_read)
        stdout_read = None
        fds_to_close.append(stdout_fd)
        stderr_fd = msvcrt.open_osfhandle(stderr_read, os.O_RDONLY | os.O_BINARY)
        handles_to_close.remove(stderr_read)
        stderr_read = None
        fds_to_close.append(stderr_fd)
        stdin_fd = msvcrt.open_osfhandle(stdin_write, os.O_WRONLY | os.O_BINARY)
        handles_to_close.remove(stdin_write)
        stdin_write = None
        fds_to_close.append(stdin_fd)
        stdout_stream = _OwnedFdStream(stdout_fd)
        fds_to_close.remove(stdout_fd)
        streams_to_close.append(stdout_stream)
        stderr_stream = _OwnedFdStream(stderr_fd)
        fds_to_close.remove(stderr_fd)
        streams_to_close.append(stderr_stream)
        stdin_stream = _OwnedFdStream(stdin_fd)
        fds_to_close.remove(stdin_fd)
        streams_to_close.append(stdin_stream)
        capture = _prepare_capture(
            stdout_stream,
            stderr_stream,
            stdin_stream,
            input_bytes=input_bytes,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        _start_capture(capture)
        cleanup_reserve = min(0.05, timeout / 3)
        execution_deadline = deadline - cleanup_reserve
        category: str | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                category = "cancelled"
                break
            if capture.overflow.is_set():
                category = "output_too_large"
                break
            accounting = BASIC_ACCOUNTING()
            if not kernel32.QueryInformationJobObject(
                wintypes.HANDLE(job_handle), 1, ctypes.byref(accounting),
                ctypes.sizeof(accounting), None
            ):
                category = "process_cleanup_failed"
                break
            main_done = kernel32.WaitForSingleObject(wintypes.HANDLE(process_handle), 0) == 0
            if main_done and accounting.ActiveProcesses == 0 and all(
                not thread.is_alive() for thread in capture.started
            ):
                break
            if monotonic() >= execution_deadline:
                category = "timeout"
                break
            sleep(0.005)

        cleanup_deadline = (
            min(deadline, monotonic() + 0.15)
            if category in {"cancelled", "output_too_large"}
            else deadline
        )
        if category is not None:
            if not cleanup_after_created_process(until=cleanup_deadline):
                category = "process_cleanup_failed"
        else:
            capture.stop.set()
            if not _finish_capture(capture, deadline=cleanup_deadline):
                category = "process_cleanup_failed"
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(wintypes.HANDLE(process_handle), ctypes.byref(exit_code)):
            category = "process_cleanup_failed"
        if capture.errors:
            category = "process_cleanup_failed"
        if category is not None:
            raise BoundedProcessError(category)
        return subprocess.CompletedProcess(
            list(argv), int(exit_code.value), bytes(capture.stdout), bytes(capture.stderr)
        )
    except BoundedProcessError:
        raise
    except BaseException as error:
        if process_handle is not None:
            cleanup_deadline = min(deadline, monotonic() + 0.15)
            cleanup_after_created_process(until=cleanup_deadline)
            raise BoundedProcessError("process_cleanup_failed") from error
        raise
    finally:
        if attribute_list_initialized:
            try:
                kernel32.DeleteProcThreadAttributeList(ctypes.cast(attribute_buffer, wintypes.LPVOID))
            except (OSError, ValueError):
                pass
        for stream in streams_to_close:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        for fd in fds_to_close:
            try:
                os.close(fd)
            except OSError:
                pass
        for handle in handles_to_close:
            close_handle(handle)
        close_handle(thread_handle)
        close_handle(process_handle)
        close_handle(job_handle)
