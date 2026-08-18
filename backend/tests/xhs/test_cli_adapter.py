import json
import subprocess
from pathlib import Path

import pytest

import backend.app.adapters.xhs_cli_read as cli_module
from backend.app.adapters.contracts import CollectionRequest
from backend.app.adapters.xhs_cli_read import (
    XhsCliAccountRequest,
    XhsCliReadAdapter,
    XhsCliSearchRequest,
)


JOB_ID = "89e3c727-47cb-417c-b59e-e32b24b15917"


class FakeRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[bytes]]) -> None:
        self._responses = iter(responses)
        self.argv: list[str] | None = None
        self.argvs: list[list[str]] = []
        self.shell: bool | None = None

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.argv = list(argv)
        self.argvs.append(self.argv)
        self.shell = kwargs.get("shell")
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


def test_search_uses_argument_array_and_json_allowlist() -> None:
    """Replacing the fixed CLI arguments with a shell string would execute untrusted input."""
    fake_runner = FakeRunner(
        [_completed(["xhs"], {"notes": [{"id": "note-1", "title": "收纳"}]})]
    )
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

    result = adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=1,
        )
    )

    assert fake_runner.argv == ["xhs", "search", "收纳", "--json"]
    assert fake_runner.shell is False
    assert result.status == "succeeded"


def test_fixed_xhs_search_syntax_keeps_json_flag_after_a_safe_query() -> None:
    """Moving --json before the positional query would no longer match the supported xhs CLI syntax."""
    fake_runner = FakeRunner([_completed(["xhs"], {"notes": []})])
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

    adapter.search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "storage boxes", "job_id": JOB_ID},
            expected_count=0,
        )
    )

    assert fake_runner.argv == ["xhs", "search", "storage boxes", "--json"]


@pytest.mark.parametrize("field", ["command", "executable", "cookie", "url", "env"])
def test_untrusted_execution_fields_are_rejected(field: str) -> None:
    """Accepting a transport-control field would let API input alter the trusted boundary."""
    with pytest.raises(ValueError):
        XhsCliSearchRequest.model_validate({"keyword": "收纳", field: "bad"})
    with pytest.raises(ValueError):
        XhsCliAccountRequest.model_validate({"user_id": "user-1", field: "bad"})


def test_fetch_account_returns_one_profile_and_requested_notes() -> None:
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
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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
        ["xhs", "user", "user-1", "--json"],
        ["xhs", "user-posts", "user-1", "--json"],
    ]


def test_fetch_account_binds_each_note_to_the_verified_profile_owner() -> None:
    fake_runner = FakeRunner(
        [
            _completed(["xhs"], {"user": {"id": "user-1"}}),
            _completed(["xhs"], {"notes": [{"id": "note-1", "title": "First"}]}),
        ]
    )
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

    result = adapter.fetch_account(
        CollectionRequest(
            capability="fetch_account",
            parameters={"user_id": "user-1", "job_id": JOB_ID},
            expected_count=2,
        )
    )

    assert result.complete is True
    assert result.items[1].data["user_id"] == "user-1"


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
    payload: dict[str, object], detail: str
) -> None:
    """A successful process exit is not collection success when the JSON envelope reports an access gate."""
    fake_runner = FakeRunner([_completed(["xhs"], payload)])
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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
    payload: dict[str, object], detail: str
) -> None:
    """An exit-zero Chinese platform gate must not be represented as an empty completed search."""
    fake_runner = FakeRunner([_completed(["xhs"], payload)])
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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
    payload: dict[str, object], detail: str
) -> None:
    """An unknown non-success envelope must not become a fabricated 0/0 result."""
    fake_runner = FakeRunner([_completed(["xhs"], payload)])
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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


def test_normal_empty_search_and_benign_note_title_remain_successful() -> None:
    """Access-gate detection must inspect envelope fields, never arbitrary note content."""
    empty_runner = FakeRunner([_completed(["xhs"], {"notes": []})])
    title_runner = FakeRunner(
        [_completed(["xhs"], {"notes": [{"id": "note-1", "title": "请先登录"}]})]
    )

    empty = XhsCliReadAdapter(executable=Path("xhs"), runner=empty_runner).search_notes(
        CollectionRequest(
            capability="search_notes",
            parameters={"keyword": "收纳", "job_id": JOB_ID},
            expected_count=0,
        )
    )
    titled = XhsCliReadAdapter(executable=Path("xhs"), runner=title_runner).search_notes(
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


def test_duplicate_notes_are_accounted_without_claiming_completion() -> None:
    """Counting a duplicate CLI row as a second note would create a false exact result."""
    fake_runner = FakeRunner(
        [
            _completed(
                ["xhs"],
                {"notes": [{"id": "same-note"}, {"id": "same-note"}]},
            )
        ]
    )
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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
def test_operational_failures_are_sanitized_needs_human_facts(marker: str) -> None:
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
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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


def test_malformed_cli_json_is_a_sanitized_incomplete_fact() -> None:
    """Treating malformed output as an empty success would fabricate collection completion."""
    fake_runner = FakeRunner(
        [subprocess.CompletedProcess(["xhs"], 0, stdout=b"{not-json", stderr=b"")]
    )
    adapter = XhsCliReadAdapter(executable=Path("xhs"), runner=fake_runner)

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
