"""Regression tests for S2A fix round 2/5."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread

import pytest

import backend.app.adapters.xhs_cli_readonly_wrapper as wrapper_module
import backend.app.adapters.xhs_cli_read as cli_module
from backend.app.adapters.contracts import CollectionRequest


_PINNED_MODULES = {
    "__init__.py": "af047caa0a1dfacaa900e7fbcc7011fd219b7325052045d077d011cd405c30a2",
    "auth.py": "771c8fa87f5776261735c3bac4c827d4d0b9b419d0d935588c7cba8a64dca6a2",
    "cli.py": "f40dd3fd431e72ec0afc412705bf00b4aadc244738db528204635df9d0801fb7",
    "client.py": "7c87b97568ff512a2b0f45fc9b74687a3f627b034b768fdd0bffbf60da9147bf",
    "exceptions.py": "6de0dca064444f6cb55c8f6186eec57d210be5bd97d763e039f7807e0d2859be",
}

_FIXTURE_SOURCES = {
    "__init__.py": b'__version__ = "0.1.4"\n',
    "exceptions.py": (
        b"class DataFetchError(Exception):\n    pass\n"
        b"class LoginError(Exception):\n    pass\n"
    ),
    "auth.py": (
        b"from .exceptions import LoginError\n"
        b'AUTH_SENTINEL = "verified-auth"\n'
    ),
    "client.py": (
        b"from .exceptions import DataFetchError, LoginError\n"
        b'CLIENT_SENTINEL = "verified-client"\n'
    ),
    "cli.py": (
        b"from . import __version__\n"
        b"from .auth import AUTH_SENTINEL\n"
        b"from .client import CLIENT_SENTINEL\n"
        b"from .exceptions import DataFetchError\n"
        b"CLI_SENTINEL = (__version__, AUTH_SENTINEL, CLIENT_SENTINEL)\n"
    ),
}


def _fixture_package(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    package = tmp_path / "xhs_cli"
    package.mkdir()
    for name, encoded in _FIXTURE_SOURCES.items():
        (package / name).write_bytes(encoded)
    manifest = {
        name: hashlib.sha256(encoded).hexdigest()
        for name, encoded in _FIXTURE_SOURCES.items()
    }
    return package, manifest


def _run_loader_probe(
    package: Path,
    manifest: dict[str, str],
    marker: Path,
    *,
    mutate_after_hash: bool,
) -> subprocess.CompletedProcess[str]:
    script = r'''
import builtins, json, pathlib, py_compile, runpy, sys

wrapper_path = pathlib.Path(sys.argv[1])
source_root = pathlib.Path(sys.argv[2])
manifest = json.loads(sys.argv[3])
marker = pathlib.Path(sys.argv[4])
mutate_after_hash = sys.argv[5] == "1"
namespace = runpy.run_path(str(wrapper_path))
loader = namespace["_load_verified_package"]
loader.__globals__["PINNED_SOURCE_SHA256"] = manifest

class AlternativeLoaderTrap:
    def find_spec(self, fullname, _path=None, _target=None):
        if fullname == "xhs_cli" or fullname.startswith("xhs_cli."):
            marker.write_text("alternative-loader-executed", encoding="utf-8")
            raise RuntimeError("alternative loader reached")
        return None

sys.meta_path.insert(0, AlternativeLoaderTrap())
cache = source_root / "__pycache__"
cache.mkdir()
malicious = source_root.parent / "malicious.py"
malicious.write_text(
    "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('pyc-executed')\n",
    encoding="utf-8",
)
py_compile.compile(str(malicious), cfile=str(cache / "cli.cpython-312.pyc"), doraise=True)
malicious.unlink()

real_compile = builtins.compile
mutated = False
def compile_from_verified_bytes(source, filename, mode, *args, **kwargs):
    global mutated
    if mutate_after_hash and not mutated and str(filename).startswith("verified-memory:"):
        mutated = True
        payload = (
            "from pathlib import Path\nPath(" + repr(str(marker))
            + ").write_text('reopened-source-executed')\n"
        )
        for path in source_root.glob("*.py"):
            path.write_text(payload, encoding="utf-8")
    return real_compile(source, filename, mode, *args, **kwargs)

builtins.compile = compile_from_verified_bytes
try:
    modules = loader(source_root)
finally:
    builtins.compile = real_compile

expected_names = {"xhs_cli", "xhs_cli.auth", "xhs_cli.cli", "xhs_cli.client", "xhs_cli.exceptions"}
actual_names = {name for name in sys.modules if name == "xhs_cli" or name.startswith("xhs_cli.")}
assert set(modules) == expected_names
assert actual_names == expected_names
assert all(str(module.__file__).startswith("verified-memory:") for module in modules.values())
assert tuple(modules["xhs_cli"].__path__) == ()
assert modules["xhs_cli.cli"].CLI_SENTINEL == (
    "0.1.4", "verified-auth", "verified-client"
)
assert not marker.exists()
print("verified-memory-loader-ok")
'''
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(Path(wrapper_module.__file__).resolve()),
            str(package),
            json.dumps(manifest, sort_keys=True),
            str(marker),
            "1" if mutate_after_hash else "0",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_pinned_manifest_covers_every_executable_package_module() -> None:
    assert wrapper_module.PINNED_SOURCE_SHA256 == _PINNED_MODULES


def test_verified_memory_loader_executes_only_the_bytes_that_were_hashed(
    tmp_path: Path,
) -> None:
    package, manifest = _fixture_package(tmp_path)
    marker = tmp_path / "unverified-code-executed.txt"

    completed = _run_loader_probe(
        package,
        manifest,
        marker,
        mutate_after_hash=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "verified-memory-loader-ok"
    assert not marker.exists()


@pytest.mark.parametrize("tampered_name", sorted(_FIXTURE_SOURCES))
def test_verified_memory_loader_rejects_each_tampered_source_without_executing_it(
    tmp_path: Path, tampered_name: str
) -> None:
    package, manifest = _fixture_package(tmp_path)
    marker = tmp_path / f"{tampered_name}-executed.txt"
    with (package / tampered_name).open("ab") as stream:
        stream.write(
            (
                "\nfrom pathlib import Path\nPath("
                + repr(str(marker))
                + ").write_text('tampered-source-executed')\n"
            ).encode("utf-8")
        )

    completed = _run_loader_probe(
        package,
        manifest,
        marker,
        mutate_after_hash=False,
    )

    assert completed.returncode != 0
    assert "pinned_source_mismatch" in completed.stderr
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point semantics")
def test_isolation_pins_each_parent_before_any_cookie_path_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside-state"
    cookie = outside / ".xhs-cli" / "cookies.json"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(
        json.dumps({"cookies": {"a1": "outside", "web_session": "outside"}}),
        encoding="utf-8",
    )
    state_link = runtime / "xhs-cli-state"
    try:
        os.symlink(outside, state_link, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    external_probe_marker = outside / "cookie-path-was-probed.txt"
    real_lstat = os.lstat

    def observed_lstat(path: object, *args: object, **kwargs: object):
        if Path(path) == state_link / ".xhs-cli" / "cookies.json":
            external_probe_marker.write_text("full path reached outside", encoding="utf-8")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", observed_lstat)
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"[]", stderr=b"")

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_link,
        runtime_dir=runtime,
        runner=runner,
    )
    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "isolation", "job_id": "round2-isolation"},
        expected_count=0,
    ))

    assert (result.status, result.detail) == (
        "needs_human",
        "external_state_untrusted",
    )
    assert calls == []
    assert not external_probe_marker.exists()
    assert not (outside / "private-runtime").exists()


def test_missing_prepared_state_fails_human_and_bounded_without_creating_it(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state = runtime / "missing-state"
    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state,
        runtime_dir=runtime,
        runner=lambda *_args, **_kwargs: pytest.fail("runner must not start"),
    )
    started = time.monotonic()

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "missing", "job_id": "round2-missing"},
        expected_count=0,
    ))

    assert time.monotonic() - started < 0.25
    assert (result.status, result.detail) == ("needs_human", "login_required")
    assert not state.exists()


def test_handle_relative_private_runtime_can_be_reopened_without_reclassification(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state = runtime / "xhs-cli-state"
    config = state / ".xhs-cli"
    config.mkdir(parents=True)
    (config / "cookies.json").write_text(
        json.dumps({"cookies": {"a1": "prepared", "web_session": "prepared"}}),
        encoding="utf-8",
    )
    calls = 0

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, stdout=b"[]", stderr=b"")

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state,
        runtime_dir=runtime,
        runner=runner,
    )
    request = CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "repeat", "job_id": "round2-repeat"},
        expected_count=0,
    )

    first = adapter.search_notes(request)
    second = adapter.search_notes(request)

    assert first.complete is True
    assert second.complete is True
    assert calls == 2


def test_staging_cleanup_boundary_refuses_formal_evidence_path(tmp_path: Path) -> None:
    from backend.app.db import Database
    from backend.app.features.xhs.service import XhsCollectionService
    from backend.app.services.jobs import JobService

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = Database(tmp_path / "workbench.db")
    database.initialize()
    service = XhsCollectionService(
        database=database,
        job_service=JobService(database),
        adapter=object(),
        runtime_dir=runtime,
        submitter=lambda *_args: None,
    )
    formal = runtime / "evidence" / "xhs" / f"{'0' * 32}.json"
    formal.parent.mkdir(parents=True)
    formal.write_bytes(b"formal-evidence-must-survive")

    with pytest.raises(OSError, match="not safely authorized"):
        service._discard_staging_path(formal)

    assert formal.read_bytes() == b"formal-evidence-must-survive"
    database.close()


def test_bounded_runner_delivers_nonempty_stdin_through_owned_raw_fd(
    tmp_path: Path,
) -> None:
    payload = b'{"cookies":{"a1":"prepared","web_session":"prepared"}}'

    completed = cli_module._run_bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        shell=False,
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=1,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        input_bytes=payload,
    )

    assert completed.returncode == 0
    assert completed.stdout == payload
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_partial_reader_start_failure_is_sanitized_and_kills_descendant_tree_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "partial-reader-descendant-survived.txt"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.45); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[1]]); "
        "time.sleep(1)"
    )
    real_start = Thread.start

    def controlled_start(thread: Thread) -> None:
        if thread.name == "xhs-cli-stderr":
            raise RuntimeError("controlled partial reader start failure")
        real_start(thread)

    monkeypatch.setattr(Thread, "start", controlled_start)
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError) as captured:
        cli_module._run_bounded_process(
            [sys.executable, "-c", parent, str(marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.3,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )

    assert captured.value.category == "process_cleanup_failed"
    assert time.monotonic() - started < 0.45
    time.sleep(0.55)
    assert not marker.exists()
    assert not any(
        thread.is_alive() and thread.name.startswith("xhs-cli-")
        for thread in __import__("threading").enumerate()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_post_create_pipe_conversion_failure_is_sanitized_and_kills_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msvcrt

    marker = tmp_path / "conversion-descendant-survived.txt"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.45); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[1]]); "
        "time.sleep(1)"
    )
    real_open = msvcrt.open_osfhandle
    calls = 0

    def fail_second_conversion(handle: int, flags: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("controlled pipe conversion failure")
        return real_open(handle, flags)

    monkeypatch.setattr(msvcrt, "open_osfhandle", fail_second_conversion)
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError) as captured:
        cli_module._run_bounded_process(
            [sys.executable, "-c", parent, str(marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.3,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )

    assert captured.value.category == "process_cleanup_failed"
    assert time.monotonic() - started < 0.45
    time.sleep(0.55)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_post_create_cleanup_never_waits_on_high_level_stream_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "blocking-close-descendant-survived.txt"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.45); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[1]]); "
        "time.sleep(1)"
    )
    real_fdopen = os.fdopen
    fdopen_calls = 0

    class BlockingCloseProxy:
        def __init__(self, stream):
            self._stream = stream

        def __getattr__(self, name: str):
            return getattr(self._stream, name)

        def close(self) -> None:
            time.sleep(0.6)
            self._stream.close()

    def blocking_first_close(*args: object, **kwargs: object):
        nonlocal fdopen_calls
        fdopen_calls += 1
        stream = real_fdopen(*args, **kwargs)
        return BlockingCloseProxy(stream) if fdopen_calls == 1 else stream

    real_start = Thread.start

    def controlled_start(thread: Thread) -> None:
        if thread.name == "xhs-cli-stderr":
            raise RuntimeError("controlled partial reader start failure")
        real_start(thread)

    monkeypatch.setattr(os, "fdopen", blocking_first_close)
    monkeypatch.setattr(Thread, "start", controlled_start)
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError) as captured:
        cli_module._run_bounded_process(
            [sys.executable, "-c", parent, str(marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.3,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )

    assert captured.value.category == "process_cleanup_failed"
    assert time.monotonic() - started < 0.45
    assert fdopen_calls == 0
    time.sleep(0.55)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_job_termination_api_failure_is_bounded_sanitized_and_kill_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    marker = tmp_path / "terminate-api-descendant-survived.txt"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.45); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[1]]); "
        "time.sleep(1)"
    )
    real_windll = ctypes.WinDLL
    real_kernel = real_windll("kernel32", use_last_error=True)

    class FailingTerminate:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            return 0

    failing_terminate = FailingTerminate()

    class KernelProxy:
        def __getattr__(self, name: str):
            if name == "TerminateJobObject":
                return failing_terminate
            return getattr(real_kernel, name)

    def controlled_windll(name: str, *args: object, **kwargs: object):
        if name.casefold() == "kernel32":
            return KernelProxy()
        return real_windll(name, *args, **kwargs)

    monkeypatch.setattr(ctypes, "WinDLL", controlled_windll)
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError) as captured:
        cli_module._run_bounded_process(
            [sys.executable, "-c", parent, str(marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.18,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )

    assert captured.value.category == "process_cleanup_failed"
    assert time.monotonic() - started < 0.4
    time.sleep(0.55)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle accounting")
def test_repeated_partial_reader_setup_failures_do_not_grow_process_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
        return int(count.value)

    real_start = Thread.start

    def controlled_start(thread: Thread) -> None:
        if thread.name == "xhs-cli-stderr":
            raise RuntimeError("controlled partial reader start failure")
        real_start(thread)

    monkeypatch.setattr(Thread, "start", controlled_start)
    # Exclude CPython's one-time Windows thread/I/O handle initialization from
    # the repeated-run leak assertion.
    with pytest.raises(cli_module.XhsCliReadError):
        cli_module._run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.3,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )
    before = handle_count()
    for _ in range(8):
        with pytest.raises(cli_module.XhsCliReadError) as captured:
            cli_module._run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                shell=False,
                cwd=tmp_path,
                env=dict(os.environ),
                timeout=0.3,
                max_stdout_bytes=4096,
                max_stderr_bytes=4096,
                input_bytes=b"",
            )
        assert captured.value.category == "process_cleanup_failed"
    after = handle_count()

    assert after <= before + 2
