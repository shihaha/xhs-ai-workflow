import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import backend.app.adapters.xhs_cli_read as cli_module
from backend.app.adapters.contracts import CollectionRequest
from backend.app.adapters.xhs_cli_read import (
    XhsCliAccountRequest,
    XhsCliReadAdapter,
    XhsCliSearchRequest,
)
from backend.app.features.xhs.redaction import redact_credentials


JOB_ID = "89e3c727-47cb-417c-b59e-e32b24b15917"
WRAPPER = str(Path(cli_module.__file__).with_name("xhs_cli_readonly_wrapper.py").resolve())


def _wrapper_argv(*command: str) -> list[str]:
    return [sys.executable, "-I", WRAPPER, *command]


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[bytes]]) -> None:
        self._responses = iter(responses)
        self.argv: list[str] | None = None
        self.argvs: list[list[str]] = []
        self.shell: bool | None = None
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.argv = list(argv)
        self.argvs.append(self.argv)
        self.shell = kwargs.get("shell")
        self.calls.append((self.argv, dict(kwargs)))
        return next(self._responses)


def _completed(
    argv: list[str], payload: object, *, returncode: int = 0
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        argv,
        returncode,
        stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stderr=b"",
    )


def _prepared_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "isolated-xhs-cli-state"
    config_dir = state_dir / ".xhs-cli"
    config_dir.mkdir(parents=True)
    (config_dir / "cookies.json").write_text(
        json.dumps({"cookies": {"a1": "prepared-a1", "web_session": "prepared-session"}}),
        encoding="utf-8",
    )
    return state_dir


@pytest.fixture
def adapter_factory(tmp_path: Path):
    state_dir = _prepared_state(tmp_path)

    def create(runner: FakeRunner, **kwargs: object) -> XhsCliReadAdapter:
        kwargs.pop("executable", None)
        return XhsCliReadAdapter(
            python_executable=kwargs.pop("python_executable", sys.executable),
            state_dir=state_dir,
            runtime_dir=tmp_path,
            runner=runner,
            **kwargs,
        )

    return create


def test_pinned_search_list_and_note_card_are_normalized(adapter_factory) -> None:
    """Treating the pinned CLI's top-level list as malformed would reject every real search."""
    xsec_secret = "xsec-search-secret-sentinel"
    fake_runner = FakeRunner([_completed(["xhs"], [{
        "id": "note-1",
        "xsecToken": xsec_secret,
        "noteCard": {
            "displayTitle": "真实标题",
            "desc": "真实公开摘要",
            "user": {"userId": "user-1", "nickname": "Alice"},
            "interactInfo": {
                "likedCount": 12,
                "collectedCount": 3,
                "commentCount": 4,
            },
            "type": "normal",
        },
    }])])
    adapter = adapter_factory(fake_runner)

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=1,
    ))

    assert (result.status, result.complete) == ("succeeded", True)
    assert result.items[0].data == {
        "note_id": "note-1",
        "title": "真实标题",
        "description": "真实公开摘要",
        "user_id": "user-1",
        "author_name": "Alice",
        "liked_count": 12,
        "collect_count": 3,
        "comment_count": 4,
        "type": "normal",
    }
    assert xsec_secret not in result.model_dump_json()


def test_pinned_user_page_and_user_posts_list_normalize_profile_stats(adapter_factory) -> None:
    """Ignoring userPageData/basicInfo and interactions would discard real profile facts."""
    fake_runner = FakeRunner([
        _completed(["xhs"], {
            "userPageData": {
                "basicInfo": {
                    "userId": "user-1",
                    "redId": "red-1",
                    "nickname": "Alice",
                    "desc": "公开简介",
                },
                "interactions": [
                    {"name": "fans", "count": 120},
                    {"name": "follows", "count": "7"},
                    {"name": "interaction", "count": 300},
                ],
            },
            "userInfo": {"userId": "user-1", "guest": False},
        }),
        _completed(["xhs"], [{
            "id": "note-1",
            "noteCard": {
                "displayTitle": "第一篇",
                "user": {"userId": "user-1", "nickname": "Alice"},
                "interactInfo": {"likedCount": 9},
            },
        }]),
    ])
    adapter = adapter_factory(fake_runner)

    result = adapter.fetch_account(CollectionRequest(
        capability="fetch_account",
        parameters={"user_id": "user-1", "job_id": JOB_ID},
        expected_count=2,
    ))

    assert (result.status, result.complete) == ("succeeded", True)
    assert result.items[0].data == {
        "user_id": "user-1",
        "red_id": "red-1",
        "nickname": "Alice",
        "bio": "公开简介",
        "followers_count": 120,
        "following_count": 7,
        "liked_count": 300,
    }
    assert result.items[1].data["title"] == "第一篇"
    assert result.items[1].data["user_id"] == "user-1"


def test_missing_isolated_external_state_fails_closed_without_starting_cli(tmp_path: Path) -> None:
    """A missing state file must not let upstream fall back to normal-browser cookie extraction."""
    fake_runner = FakeRunner([_completed(["xhs"], [])])
    adapter = XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=tmp_path / "isolated-state",
        runtime_dir=tmp_path,
        runner=fake_runner,
    )

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=1,
    ))

    assert (result.status, result.detail, result.complete) == (
        "needs_human", "login_required", False
    )
    assert fake_runner.calls == []


def test_child_receives_only_explicit_isolated_state_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inheriting the parent environment could expose normal browser profiles and secrets."""
    state_dir = _prepared_state(tmp_path)
    monkeypatch.setenv("PARENT_SECRET_SENTINEL", "must-not-reach-child")
    fake_runner = FakeRunner([_completed(["xhs"], [])])
    adapter = XhsCliReadAdapter(
        python_executable=sys.executable,
        state_dir=state_dir,
        runtime_dir=tmp_path,
        runner=fake_runner,
    )

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=0,
    ))

    assert result.complete is True
    _, kwargs = fake_runner.calls[0]
    assert kwargs["shell"] is False
    private_runtime = state_dir.resolve() / "private-runtime"
    assert Path(str(kwargs["cwd"])).resolve() == private_runtime
    child_env = kwargs["env"]
    assert isinstance(child_env, dict)
    assert child_env["HOME"] == str(private_runtime)
    assert child_env["USERPROFILE"] == str(private_runtime)
    assert Path(child_env["APPDATA"]).is_relative_to(state_dir.resolve())
    assert Path(child_env["LOCALAPPDATA"]).is_relative_to(state_dir.resolve())
    assert "PARENT_SECRET_SENTINEL" not in child_env


def test_actual_credential_containers_and_case_variants_are_redacted() -> None:
    """Case-sensitive Name/Value handling or named-cookie allowlists leak real credential shapes."""
    secret = "actual-shape-secret-sentinel"
    ordinary = "ordinary-content-sentinel"
    redacted = redact_credentials({
        "Headers": [
            {"Name": "Cookie", "Value": secret},
            {"NAME": "Authorization", "VALUE": secret},
            {"Name": "title", "Value": ordinary},
        ],
        "Cookies": {"totally_arbitrary_cookie_name": secret},
        "tokens": {"provider_specific_name": secret},
        "xsecToken": secret,
        "xsec_token": secret,
        "noteCard": {"displayTitle": ordinary},
    })

    rendered = json.dumps(redacted, ensure_ascii=False)
    assert secret not in rendered
    assert ordinary in rendered


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_bounded_process_kills_output_overflow_during_execution(
    tmp_path: Path, stream_name: str
) -> None:
    """Checking size only after process exit allows unbounded memory growth."""
    late_marker = tmp_path / f"late-{stream_name}.txt"
    script = (
        "import pathlib,sys,time; "
        f"stream=sys.{stream_name}.buffer; "
        "stream.write(b'x'*1048576); stream.flush(); "
        "time.sleep(1.5); pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError, match="output_too_large"):
        cli_module._run_bounded_process(
            [sys.executable, "-c", script, str(late_marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=5.0,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert time.monotonic() - started < 1.5
    time.sleep(0.1)
    assert not late_marker.exists()


def test_bounded_process_drains_both_streams_before_closing_handles(tmp_path: Path) -> None:
    """A successful child must not lose buffered bytes during pipe cleanup."""
    size = 512 * 1024
    script = (
        "import sys; "
        f"sys.stdout.buffer.write(b'o'*{size}); sys.stdout.buffer.flush(); "
        f"sys.stderr.buffer.write(b'e'*{size}); sys.stderr.buffer.flush()"
    )

    completed = cli_module._run_bounded_process(
        [sys.executable, "-c", script],
        shell=False,
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5.0,
        max_stdout_bytes=size,
        max_stderr_bytes=size,
    )

    assert completed.stdout == b"o" * size
    assert completed.stderr == b"e" * size


def test_bounded_process_kills_timeout_before_child_can_continue(tmp_path: Path) -> None:
    """Returning a timeout while leaving the child alive would violate bounded execution."""
    late_marker = tmp_path / "late-timeout.txt"
    script = (
        "import pathlib,sys,time; time.sleep(1.5); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    started = time.monotonic()

    with pytest.raises(cli_module.XhsCliReadError, match="timeout"):
        cli_module._run_bounded_process(
            [sys.executable, "-c", script, str(late_marker)],
            shell=False,
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.1,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert time.monotonic() - started < 1.5
    time.sleep(0.1)
    assert not late_marker.exists()


def test_search_uses_argument_array_and_json_allowlist(adapter_factory) -> None:
    """Replacing the fixed CLI arguments with a shell string would execute untrusted input."""
    fake_runner = FakeRunner(
        [_completed(["xhs"], {"notes": [{"id": "note-1", "title": "收纳"}]})]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=1,
        )
    )

    assert fake_runner.argv == _wrapper_argv("search", "收纳", "--json")
    assert fake_runner.shell is False
    assert result.status == "succeeded"


def test_fixed_xhs_search_syntax_keeps_json_flag_after_a_safe_query(adapter_factory) -> None:
    """Moving --json before the positional query would no longer match the supported xhs CLI syntax."""
    fake_runner = FakeRunner([_completed(["xhs"], {"notes": []})])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "storage boxes", "job_id": JOB_ID},
            expected_count=0,
        )
    )

    assert fake_runner.argv == _wrapper_argv("search", "storage boxes", "--json")


@pytest.mark.parametrize("field", ["command", "executable", "cookie", "url", "env"])
def test_untrusted_execution_fields_are_rejected(field: str) -> None:
    """Accepting a transport-control field would let API input alter the trusted boundary."""
    with pytest.raises(ValueError):
        XhsCliSearchRequest.model_validate({"keyword": "收纳", field: "bad"})
    with pytest.raises(ValueError):
        XhsCliAccountRequest.model_validate({"user_id": "user-1", field: "bad"})


def test_fetch_account_returns_one_profile_and_requested_notes(adapter_factory) -> None:
    """Dropping the profile would falsely let the service's N+1 count look complete."""
    fake_runner = FakeRunner(
        [
            _completed(["xhs"], {"user": {"id": "user-1", "nickname": "Alice"}}),
            _completed(
                ["xhs"],
                {"notes": [{"id": "note-1", "title": "First", "user_id": "user-1"}]},
            ),
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.fetch_account(
        CollectionRequest(
            capability="fetch_account",
            parameters={"user_id": "user-1", "job_id": JOB_ID},
            expected_count=2,
        )
    )

    assert [item.kind for item in result.items] == ["profile", "note"]
    assert result.items[0].id == "profile:user-1"
    assert result.items[1].id == "note:note-1"
    assert str(result.items[1].source_url) == "https://www.xiaohongshu.com/explore/note-1"
    assert result.status == "succeeded"
    assert result.complete is True
    assert fake_runner.argvs == [
        _wrapper_argv("user", "user-1", "--json"),
        _wrapper_argv("user-posts", "user-1", "--json"),
    ]


def test_fetch_account_binds_each_note_to_the_verified_profile_owner(adapter_factory) -> None:
    fake_runner = FakeRunner(
        [
            _completed(["xhs"], {"user": {"id": "user-1"}}),
            _completed(["xhs"], {"notes": [{"id": "note-1", "title": "First"}]}),
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.fetch_account(
        CollectionRequest(
            capability="fetch_account",
            parameters={"user_id": "user-1", "job_id": JOB_ID},
            expected_count=2,
        )
    )

    assert result.complete is True
    assert result.items[1].data["user_id"] == "user-1"


def test_fetch_account_accepts_matching_user_id_alias_for_the_verified_owner(adapter_factory) -> None:
    fake_runner = FakeRunner(
        [
            _completed(["xhs"], {"user": {"id": "user-1"}}),
            _completed(["xhs"], {"notes": [{"id": "note-1", "userId": "user-1"}]}),
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.fetch_account(
        CollectionRequest(
            capability="fetch_account",
            parameters={"user_id": "user-1", "job_id": JOB_ID},
            expected_count=2,
        )
    )

    assert result.complete is True
    assert result.items[1].data["user_id"] == "user-1"
    assert result.items[1].data["userId"] == "user-1"


@pytest.mark.parametrize(
    "note",
    [
        {"id": "note-1", "userId": "user-2"},
        {"id": "note-1", "user_id": "user-1", "userId": "user-2"},
        {"id": "note-1", "author": {"userId": "user-2"}},
    ],
)
def test_fetch_account_rejects_conflicting_or_cross_account_owner_aliases(
    note: dict[str, object],
    adapter_factory,
) -> None:
    fake_runner = FakeRunner(
        [
            _completed(["xhs"], {"user": {"id": "user-1"}}),
            _completed(["xhs"], {"notes": [note]}),
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.fetch_account(
        CollectionRequest(
            capability="fetch_account",
            parameters={"user_id": "user-1", "job_id": JOB_ID},
            expected_count=2,
        )
    )

    assert result.complete is False
    assert result.status == "needs_human"
    assert [item.reason for item in result.rejected_items] == ["note_owner_mismatch"]


def test_fetch_account_rejects_conflicting_profile_owner_aliases(adapter_factory) -> None:
    fake_runner = FakeRunner(
        [
            _completed(["xhs"], {"user": {"id": "user-1", "userId": "user-2"}}),
            _completed(["xhs"], {"notes": []}),
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.fetch_account(
        CollectionRequest(
            capability="fetch_account",
            parameters={"user_id": "user-1", "job_id": JOB_ID},
            expected_count=1,
        )
    )

    assert result.complete is False
    assert result.status == "needs_human"
    assert result.rejected_items[0].reason == "profile_identity_conflict"


def test_search_rejects_conflicting_owner_aliases(adapter_factory) -> None:
    fake_runner = FakeRunner([
        _completed(["xhs"], {"notes": [{
            "id": "note-1", "user_id": "user-1", "userId": "user-2"
        }]})
    ])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=1,
    ))

    assert result.status == "needs_human"
    assert result.complete is False
    assert [item.reason for item in result.rejected_items] == ["note_owner_conflict"]


def test_structured_header_name_value_credentials_are_redacted_without_false_positive(adapter_factory) -> None:
    secret = "structured-secret-sentinel"
    ordinary = "ordinary-value-sentinel"
    session_title = "session-title-is-public"
    secret_garden = "secret-garden-is-public"
    fake_runner = FakeRunner([_completed(["xhs"], {"notes": [{
        "id": "note-1",
        "headers": [
            {"name": "Cookie", "value": secret},
            {"name": "Authorization", "value": secret},
            {"name": "access_token", "value": secret},
            {"name": "title", "value": ordinary},
            {"name": "session title", "value": session_title},
            {"name": "secret garden", "value": secret_garden},
        ],
    }]})])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=1,
    ))

    rendered = result.model_dump_json()
    assert secret not in rendered
    assert ordinary in rendered
    assert session_title in rendered
    assert secret_garden in rendered


@pytest.mark.parametrize(
    "credential_name",
    [
        "cookie",
        "auth",
        "authorization",
        "csrf",
        "xsrf",
        "bearer",
        "jwt",
        "token",
        "password",
        "secret",
        "accessToken",
        "access-token",
        "access token",
        "refreshToken",
        "AccessToken",
        "RefreshToken",
        "apiToken",
        "oauthToken",
        "OAuthToken",
        "XOAuthToken",
        "auth_token",
        "auth-token",
        "auth token",
        "personalAccessToken",
        "PersonalAccessToken",
        "cookie_string",
        "cookieString",
        "cookie-string",
        "cookie string",
        "clientSecret",
        "client_secret",
        "apiKey",
        "api_key",
        "APIKey",
        "XAPIKey",
        "apiSecret",
        "csrfToken",
        "CSRFToken",
        "XCSRFToken",
        "csrf_token",
        "csrf-token",
        "csrf token",
        "xsrfToken",
        "bearerToken",
        "jwtToken",
        "JWTToken",
        "URLToken",
        "sessionId",
        "SessionID",
        "session-id",
        "session id",
        "sessionToken",
        "sessionCookie",
        "session_cookie",
        "webSession",
        "WebSession",
        "web_session",
        "x-api-token",
        "x-oauth-token",
        "x-session-id",
    ],
)
def test_direct_and_structured_credential_aliases_are_redacted(
    credential_name: str,
    adapter_factory,
) -> None:
    direct_secret = f"direct-{credential_name}-secret-sentinel"
    structured_secret = f"structured-{credential_name}-secret-sentinel"
    fake_runner = FakeRunner([_completed(["xhs"], {"notes": [{
        "id": "note-1",
        credential_name: direct_secret,
        "headers": [{"name": credential_name, "value": structured_secret}],
    }]})])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=1,
    ))

    rendered = result.model_dump_json()
    assert direct_secret not in rendered
    assert structured_secret not in rendered


@pytest.mark.parametrize(
    "ordinary_name",
    [
        "access",
        "refresh",
        "session",
        "session title",
        "secret garden",
        "tokenCount",
        "access level",
        "refresh rate",
        "csrf protection",
        "xsrf guide",
        "bearer profile",
        "jwt handbook",
        "api key note",
        "api token count",
        "oauth token status",
        "personal access level",
        "web session title",
        "client secret garden",
        "x session title",
    ],
)
def test_noncredential_full_token_sequences_are_preserved(
    ordinary_name: str,
    adapter_factory,
) -> None:
    direct_value = f"direct-{ordinary_name}-public-sentinel"
    structured_value = f"structured-{ordinary_name}-public-sentinel"
    fake_runner = FakeRunner([_completed(["xhs"], {"notes": [{
        "id": "note-1",
        ordinary_name: direct_value,
        "headers": [{"name": ordinary_name, "value": structured_value}],
    }]})])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(CollectionRequest(
        capability="search_notes",
        parameters={"keyword": "收纳", "job_id": JOB_ID},
        expected_count=1,
    ))

    rendered = result.model_dump_json()
    assert direct_value in rendered
    assert structured_value in rendered


@pytest.mark.parametrize("field", ["keyword", "user_id"])
@pytest.mark.parametrize("unsafe_value", ["--json", "-x", "line\nbreak", "nul\x00byte"])
def test_positional_cli_values_reject_options_and_control_characters(
    field: str, unsafe_value: str
) -> None:
    """A positional value parsed as an option/control sequence could change the fixed CLI command."""
    schema = XhsCliSearchRequest if field == "keyword" else XhsCliAccountRequest

    with pytest.raises(ValueError):
        schema.model_validate({field: unsafe_value})


@pytest.mark.parametrize(
    "payload, detail",
    [
        ({"notes": [], "message": "login required"}, "login_required"),
        ({"notes": [], "message": "captcha required"}, "captcha_required"),
        ({"notes": [], "message": "rate limit exceeded"}, "rate_limited"),
        (
            {"data": {"notes": [], "message": "captcha required"}},
            "captcha_required",
        ),
    ],
)
def test_exit_zero_auth_envelopes_cannot_claim_empty_success(
    payload: dict[str, object], detail: str, adapter_factory
) -> None:
    """A successful process exit is not collection success when the JSON envelope reports an access gate."""
    fake_runner = FakeRunner([_completed(["xhs"], payload)])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=0,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == detail
    assert result.complete is False


@pytest.mark.parametrize(
    "payload, detail",
    [
        ({"notes": [], "message": "请先登录"}, "login_required"),
        ({"notes": [], "message": "登录已过期"}, "login_required"),
        ({"notes": [], "message": "需要验证"}, "captcha_required"),
        ({"notes": [], "message": "验证码"}, "captcha_required"),
        ({"notes": [], "message": "请求频繁"}, "rate_limited"),
        ({"notes": [], "message": "访问受限"}, "account_visibility_restricted"),
        ({"data": {"notes": [], "message": "请先登录"}}, "login_required"),
        ({"data": {"notes": [], "status": "登录已过期"}}, "login_required"),
        ({"data": {"notes": [], "error": "需要验证"}}, "captcha_required"),
        ({"data": {"notes": [], "message": "验证码"}}, "captcha_required"),
        ({"data": {"notes": [], "status": "请求频繁"}}, "rate_limited"),
        ({"data": {"notes": [], "error": "访问受限"}}, "account_visibility_restricted"),
    ],
)
def test_exit_zero_chinese_access_gates_are_needs_human_facts(
    payload: dict[str, object], detail: str, adapter_factory
) -> None:
    """An exit-zero Chinese platform gate must not be represented as an empty completed search."""
    fake_runner = FakeRunner([_completed(["xhs"], payload)])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=0,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == detail
    assert result.complete is False


@pytest.mark.parametrize(
    "payload, detail",
    [
        ({"notes": [], "status": 401}, "login_required"),
        ({"data": {"notes": [], "status": 429}}, "rate_limited"),
        ({"data": {"notes": [], "error": {"code": 403}}}, "account_visibility_restricted"),
        ({"notes": [], "error": "unrecognized provider failure"}, "response_unusable"),
        ({"data": {"notes": [], "status": "unexpected"}}, "response_unusable"),
    ],
)
def test_exit_zero_error_or_status_envelopes_fail_closed(
    payload: dict[str, object], detail: str, adapter_factory
) -> None:
    """An unknown non-success envelope must not become a fabricated 0/0 result."""
    fake_runner = FakeRunner([_completed(["xhs"], payload)])
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=0,
        )
    )

    assert result.status == "needs_human"
    assert result.detail == detail
    assert result.complete is False


def test_normal_empty_search_and_benign_note_title_remain_successful(adapter_factory) -> None:
    """Access-gate detection must inspect envelope fields, never arbitrary note content."""
    empty_runner = FakeRunner([_completed(["xhs"], {"notes": []})])
    title_runner = FakeRunner(
        [_completed(["xhs"], {"notes": [{"id": "note-1", "title": "请先登录"}]})]
    )

    empty = adapter_factory(empty_runner, executable=Path("xhs")).search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=0,
        )
    )
    titled = adapter_factory(title_runner, executable=Path("xhs")).search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=1,
        )
    )

    assert (empty.status, empty.complete) == ("succeeded", True)
    assert (titled.status, titled.complete) == ("succeeded", True)


def test_module_exposes_no_public_arbitrary_argv_runner() -> None:
    """A public argv runner would let future callers bypass the adapter's command allowlist."""
    assert not hasattr(cli_module, "run_xhs_json")


def test_duplicate_notes_are_accounted_without_claiming_completion(adapter_factory) -> None:
    """Counting a duplicate CLI row as a second note would create a false exact result."""
    fake_runner = FakeRunner(
        [
            _completed(
                ["xhs"],
                {"notes": [{"id": "same-note"}, {"id": "same-note"}]},
            )
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=2,
        )
    )

    assert result.status == "partial"
    assert result.complete is False
    assert result.duplicate_observation_count == 1
    assert result.missing_items[0].reason == "expected_item_not_observed"


@pytest.mark.parametrize("marker", ["login required", "captcha required", "rate limit"])
def test_operational_failures_are_sanitized_needs_human_facts(marker: str, adapter_factory) -> None:
    """Returning CLI stderr verbatim would leak session credentials into durable job facts."""
    secret = "cookie-sentinel-token-sentinel"
    fake_runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                ["xhs"],
                1,
                stdout=f"{marker}; cookie={secret}".encode(),
                stderr=f"token={secret}".encode(),
            )
        ]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=1,
        )
    )

    assert result.status == "needs_human"
    assert result.detail in {"login_required", "captcha_required", "rate_limited"}
    assert secret not in result.model_dump_json()


def test_malformed_cli_json_is_a_sanitized_incomplete_fact(adapter_factory) -> None:
    """Treating malformed output as an empty success would fabricate collection completion."""
    fake_runner = FakeRunner(
        [subprocess.CompletedProcess(["xhs"], 0, stdout=b"{not-json", stderr=b"")]
    )
    adapter = adapter_factory(fake_runner, executable=Path("xhs"))

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=1,
        )
    )

    assert result.status == "failed"
    assert result.detail == "malformed_output"
    assert result.complete is False
