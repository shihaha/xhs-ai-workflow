"""Regression tests for the second S2A trust-boundary review."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import backend.app.adapters.xhs_cli_read as cli_module
from backend.app.adapters.contracts import CollectionItem, CollectionRequest, CollectionResult
from backend.app.db import Database
from backend.app.features.xhs.models import XhsAccountNoteRecord, XhsAccountProfileRecord
from backend.app.features.xhs.service import XhsCollectionService
from backend.app.main import _lifespan
from backend.app.models.jobs import JobState
from backend.app.services.jobs import JobService


def _state(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "runtime" / "xhs-cli-state"
    cookie_file = state_dir / ".xhs-cli" / "cookies.json"
    cookie_file.parent.mkdir(parents=True)
    cookie_file.write_text(
        json.dumps({"cookies": {"a1": "prepared-a1", "web_session": "prepared-session"}}),
        encoding="utf-8",
    )
    return state_dir, cookie_file


def _completed(argv: list[str], payload: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stderr=b"",
    )


def _search_request(expected_count: int = 0) -> CollectionRequest:
    return CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": "round1-job"},
        expected_count=expected_count,
    )


def test_adapter_launches_only_the_pinned_readonly_wrapper_and_pipes_credentials(
    tmp_path: Path,
) -> None:
    state_dir, _ = _state(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(argv), dict(kwargs)))
        return _completed(argv, [])

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=runner,
    )
    result = adapter.search_notes(_search_request())

    assert result.complete is True
    argv, kwargs = calls[0]
    wrapper = Path(importlib.import_module(
        "backend.app.adapters.xhs_cli_readonly_wrapper"
    ).__file__).resolve()
    assert argv == [sys.executable, "-I", str(wrapper), "search", "收纳", "--json"]
    assert kwargs["shell"] is False
    assert json.loads(kwargs["input_bytes"])["cookies"]["a1"] == "prepared-a1"
    assert "prepared-a1" not in " ".join(argv)
    assert "prepared-a1" not in json.dumps(kwargs["env"])
    assert Path(str(kwargs["cwd"])).is_relative_to(state_dir / "private-runtime")
    assert Path(str(kwargs["env"]["HOME"])).is_relative_to(state_dir / "private-runtime")


def test_readonly_wrapper_disables_browser_auth_and_token_cache_writes() -> None:
    wrapper = importlib.import_module("backend.app.adapters.xhs_cli_readonly_wrapper")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("browser/profile or cache function was reachable")

    auth = SimpleNamespace(
        get_cookie_string=forbidden,
        get_saved_cookie_string=forbidden,
        _load_saved_cookies=forbidden,
        _extract_browser_cookies=forbidden,
        qrcode_login=forbidden,
        _browser_assisted_qrcode_login=forbidden,
        save_cookies=forbidden,
        clear_cookies=forbidden,
        save_token_cache=forbidden,
        load_xsec_token=forbidden,
    )
    cli = SimpleNamespace(
        get_cookie_string=forbidden,
        get_saved_cookie_string=forbidden,
        qrcode_login=forbidden,
        clear_cookies=forbidden,
        save_token_cache=forbidden,
        load_xsec_token=forbidden,
    )

    wrapper._install_readonly_boundary(
        cli,
        auth,
        {"a1": "prepared-a1", "web_session": "prepared-session", "other": "value"},
    )

    assert "a1=prepared-a1" in cli.get_cookie_string()
    assert cli.get_saved_cookie_string() == cli.get_cookie_string()
    cli.save_token_cache({"note": "xsec-secret"})
    auth.save_token_cache({"note": "xsec-secret"})
    with pytest.raises(RuntimeError, match="disabled"):
        auth._extract_browser_cookies()
    with pytest.raises(RuntimeError, match="disabled"):
        cli.qrcode_login()


def test_cookie_hardlink_fails_closed_before_process_start(tmp_path: Path) -> None:
    state_dir, cookie_file = _state(tmp_path)
    os.link(cookie_file, cookie_file.with_name("cookies-hardlink.json"))
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return _completed(argv, [])

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=runner,
    )
    result = adapter.search_notes(_search_request())

    assert (result.status, result.detail) == ("needs_human", "external_state_untrusted")
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point semantics")
def test_state_directory_reparse_fails_before_writing_private_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside-state"
    cookie = outside / ".xhs-cli" / "cookies.json"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(
        json.dumps({"cookies": {"a1": "prepared-a1", "web_session": "prepared-session"}}),
        encoding="utf-8",
    )
    state_link = runtime / "xhs-cli-state"
    try:
        os.symlink(outside, state_link, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return _completed(argv, [])

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_link,
        runtime_dir=runtime,
        runner=runner,
    )
    result = adapter.search_notes(_search_request())

    assert (result.status, result.detail) == ("needs_human", "external_state_untrusted")
    assert calls == []
    assert not (outside / "private-runtime").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point semantics")
def test_private_runtime_reparse_fails_before_touching_its_target(tmp_path: Path) -> None:
    state_dir, _ = _state(tmp_path)
    outside = tmp_path / "outside-private"
    outside.mkdir()
    try:
        os.symlink(outside, state_dir / "private-runtime", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return _completed(argv, [])

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=runner,
    )
    result = adapter.search_notes(_search_request())

    assert (result.status, result.detail) == ("needs_human", "external_state_untrusted")
    assert calls == []
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows handle share semantics")
def test_cookie_identity_cannot_be_swapped_while_command_is_running(tmp_path: Path) -> None:
    state_dir, cookie_file = _state(tmp_path)
    replacement = cookie_file.with_name("replacement.json")
    replacement.write_text(
        json.dumps({"cookies": {"a1": "attacker", "web_session": "attacker"}}),
        encoding="utf-8",
    )

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        with pytest.raises(PermissionError):
            os.replace(replacement, cookie_file)
        return _completed(argv, [])

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=runner,
    )

    assert adapter.search_notes(_search_request()).complete is True
    assert replacement.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
@pytest.mark.parametrize("inherits_pipe", [False, True])
def test_bounded_runner_kills_descendant_after_direct_child_exits(
    tmp_path: Path, inherits_pipe: bool
) -> None:
    marker = tmp_path / f"descendant-{inherits_pipe}.txt"
    child = (
        "import pathlib,sys,time; time.sleep(0.8); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    stream_args = "" if inherits_pipe else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{child!r},sys.argv[1]]{stream_args})"
    )
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError, match="timeout"):
        cli_module._run_bounded_process(
            [sys.executable, "-c", parent, str(marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.15,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )

    assert time.monotonic() - started < 0.7
    time.sleep(0.85)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_runner_cancellation_terminates_tree_within_close_budget(tmp_path: Path) -> None:
    marker = tmp_path / "cancelled-descendant.txt"
    started_marker = tmp_path / "started.txt"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.8); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[2]]); "
        "pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(5)"
    )
    cancel = Event()
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            cli_module._run_bounded_process(
                [sys.executable, "-c", parent, str(started_marker), str(marker)],
                shell=False,
                cwd=tmp_path,
                env=dict(os.environ),
                timeout=5.0,
                max_stdout_bytes=4096,
                max_stderr_bytes=4096,
                input_bytes=b"",
                cancel_event=cancel,
            )
        except BaseException as error:
            outcome.append(error)

    from threading import Thread

    thread = Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 2
    while not started_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started_marker.exists()
    cancelled_at = time.monotonic()
    cancel.set()
    thread.join(0.25)

    assert not thread.is_alive()
    assert time.monotonic() - cancelled_at < 0.25
    assert len(outcome) == 1 and isinstance(outcome[0], cli_module.XhsCliReadError)
    assert outcome[0].category == "cancelled"
    time.sleep(0.85)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle accounting")
def test_normal_runner_completion_does_not_leak_process_or_pipe_handles(tmp_path: Path) -> None:
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

    before = handle_count()
    for _ in range(12):
        completed = cli_module._run_bounded_process(
            [sys.executable, "-c", "print('ok')"],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=2.0,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
            input_bytes=b"",
        )
        assert completed.stdout == b"ok\r\n"
    after = handle_count()

    assert after <= before + 2


@pytest.mark.skipif(os.name != "nt", reason="Windows handle accounting")
def test_runner_setup_failure_does_not_leak_converted_pipe_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes
    import msvcrt
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

    real_open = msvcrt.open_osfhandle
    calls = 0

    def fail_every_second_conversion(handle: int, flags: int) -> int:
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise OSError("controlled fd conversion failure")
        return real_open(handle, flags)

    monkeypatch.setattr(msvcrt, "open_osfhandle", fail_every_second_conversion)
    before = handle_count()
    for _ in range(6):
        with pytest.raises(OSError, match="controlled fd conversion failure"):
            cli_module._run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                shell=False,
                cwd=tmp_path,
                env=dict(os.environ),
                timeout=2.0,
                max_stdout_bytes=4096,
                max_stderr_bytes=4096,
                input_bytes=b"",
            )
    after = handle_count()

    assert after <= before + 2


def test_one_thousand_rows_keep_one_shared_raw_response_and_linear_size(tmp_path: Path) -> None:
    state_dir, _ = _state(tmp_path)
    payload = [
        {"id": f"note-{index:04d}", "noteCard": {"displayTitle": f"row-{index:04d}"}}
        for index in range(1000)
    ]

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return _completed(argv, payload)

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=runner,
    )
    result = adapter.search_notes(_search_request(expected_count=1000))
    encoded = result.model_dump_json().encode("utf-8")

    assert (result.status, result.succeeded_count, result.complete) == ("succeeded", 1000, True)
    assert result.raw_evidence["responses"]["search"] == payload
    assert all("response" not in item.raw_evidence for item in result.items)
    assert all(item.raw_evidence["row_index"] == index for index, item in enumerate(result.items))
    assert len(encoded) < 1_000_000


def test_transformed_result_over_hard_cap_cannot_report_success(tmp_path: Path) -> None:
    state_dir, _ = _state(tmp_path)
    payload = [{"id": "note-1", "public_blob": "x" * 700_000}]

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return _completed(argv, payload)

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=runner,
        max_stdout_bytes=1_000_000,
    )

    result = adapter.search_notes(_search_request(expected_count=1))

    assert (result.status, result.detail, result.complete) == (
        "failed",
        "result_too_large",
        False,
    )


def _search_result(*, blob: str = "") -> CollectionResult:
    return CollectionResult(
        status="succeeded",
        raw_evidence={"responses": {"search": [{"id": "note-1", "blob": blob}]}},
        items=[CollectionItem(
            id="note:note-1",
            kind="note",
            source_url="https://www.xiaohongshu.com/explore/note-1",
            raw_evidence={"response_ref": "search:test", "row_index": 0, "row": {"id": "note-1"}},
            data={"note_id": "note-1"},
        )],
        expected_count_known=True,
        expected_count=1,
        succeeded_count=1,
        observed_count=1,
        overflow_count=0,
        complete=True,
    )


def _service(tmp_path: Path, adapter: object, **kwargs: object) -> XhsCollectionService:
    runtime = tmp_path / "service-runtime"
    database = Database(runtime / "workbench.sqlite3", runtime_dir=runtime)
    jobs = JobService(database, runtime_dir=runtime)
    return XhsCollectionService(
        database=database,
        job_service=jobs,
        adapter=adapter,
        runtime_dir=runtime,
        **kwargs,
    )


def test_service_close_propagates_cancel_and_leaves_no_late_facts(tmp_path: Path) -> None:
    started = Event()
    cancelled = Event()
    stopped = Event()

    class Adapter:
        def search_notes(self, _request: CollectionRequest) -> CollectionResult:
            started.set()
            cancelled.wait(2)
            stopped.set()
            return _search_result()

        def close(self, *, timeout: float) -> bool:
            cancelled.set()
            return stopped.wait(timeout)

    service = _service(tmp_path, Adapter())
    job = service.submit_search("收纳", 1)
    assert started.wait(1)
    close_started = time.monotonic()
    try:
        assert service.close() is True
    finally:
        cancelled.set()
    assert time.monotonic() - close_started <= 0.25
    assert service.job_service.get(job.id).state is JobState.cancelled
    assert service.job_service.get(job.id).artifacts == []
    with service.database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []
    service.database.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifecycle")
def test_service_close_cancels_real_adapter_runner_and_descendant_tree(tmp_path: Path) -> None:
    state_dir, _ = _state(tmp_path)
    started = tmp_path / "adapter-tree-started.txt"
    late_marker = tmp_path / "adapter-tree-survived.txt"
    descendant = (
        "import pathlib,sys,time; time.sleep(0.8); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[2]]); "
        "pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(5)"
    )

    def controlled_runner(_argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return cli_module._run_bounded_process(
            [sys.executable, "-c", parent, str(started), str(late_marker)],
            shell=False,
            cwd=kwargs["cwd"],
            env=kwargs["env"],
            timeout=kwargs["timeout"],
            max_stdout_bytes=kwargs["max_stdout_bytes"],
            max_stderr_bytes=kwargs["max_stderr_bytes"],
            input_bytes=b"",
            cancel_event=kwargs["cancel_event"],
        )

    adapter = cli_module.XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        runner=controlled_runner,
        timeout_seconds=5,
    )
    service = _service(tmp_path, adapter)
    job = service.submit_search("收纳", 1)
    deadline = time.monotonic() + 2
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()

    close_started = time.monotonic()
    assert service.close() is True
    assert time.monotonic() - close_started <= 0.25
    assert service.job_service.get(job.id).state is JobState.cancelled
    assert service.job_service.get(job.id).artifacts == []
    time.sleep(0.85)
    assert not late_marker.exists()
    with service.database.session() as session:
        assert session.scalars(select(XhsAccountProfileRecord)).all() == []
        assert session.scalars(select(XhsAccountNoteRecord)).all() == []
    service.database.close()


def test_oversized_artifact_is_failure_only_and_never_commits_success(tmp_path: Path) -> None:
    class Adapter:
        def search_notes(self, _request: CollectionRequest) -> CollectionResult:
            return _search_result(blob="x" * 4096)

    service = _service(
        tmp_path,
        Adapter(),
        submitter=lambda *_args: None,
        max_artifact_bytes=1024,
    )
    job = service.submit_search("收纳", 1)
    completed = service.execute(job.id)

    assert completed is not None and completed.state is JobState.failed
    assert completed.error_category == "xhs_collection_result_too_large"
    assert len(completed.artifacts) == 1
    assert completed.artifacts[0].metadata.get("complete") is not True
    assert not (service.runtime_dir / "evidence" / "xhs" / f"{job.id}.json").exists()
    service.database.close()


@pytest.mark.anyio
async def test_lifespan_does_not_ignore_unsafe_xhs_close() -> None:
    closed: list[str] = []

    class UnsafeXhs:
        def close(self) -> bool:
            closed.append("xhs")
            return False

    class Other:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    app = SimpleNamespace(state=SimpleNamespace(
        artifact_cleanup_worker=None,
        xhs_collection_service=UnsafeXhs(),
        qianfan_collection_service=Other("qianfan"),
        shop_service=Other("shop"),
        database=Other("database"),
    ))

    with pytest.raises(RuntimeError, match="XHS collection process tree did not stop"):
        async with _lifespan(app):
            pass

    assert closed == ["xhs", "qianfan", "shop", "database"]
