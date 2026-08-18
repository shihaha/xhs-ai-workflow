import json
import subprocess
from pathlib import Path

import pytest

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
        self.shell: bool | None = None

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.argv = list(argv)
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
