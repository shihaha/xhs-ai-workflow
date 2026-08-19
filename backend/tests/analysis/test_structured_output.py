import json

import httpx
import pytest

from backend.app.adapters.bailian import (
    BailianModelAdapter,
    ModelOutputInvalid,
)
from backend.app.adapters.contracts import StructuredModelRequest
from backend.app.features.analysis.schemas import AnalysisOutput


def _response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
    )


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        system_prompt="Return JSON only.",
        user_prompt="Analyze persisted evidence.",
        prompt_version="analysis-v1",
        evidence_ids=["artifact:1"],
    )


def test_adapter_validates_strict_structured_json() -> None:
    output = {
        "claims": [{"claim": "需求存在", "evidence_ids": ["artifact:1"]}],
        "product_clusters": [],
        "opportunities": [],
    }
    transport = httpx.MockTransport(lambda _: _response(json.dumps(output, ensure_ascii=False)))
    adapter = BailianModelAdapter(
        api_key="secret",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="deepseek-v3",
        transport=transport,
    )

    result = adapter.generate_structured(_request(), AnalysisOutput)

    assert result.output == output
    assert result.usage["total_tokens"] == 7
    assert "secret" not in json.dumps(result.raw_evidence)


def test_adapter_sends_fixed_exact_json_schema_instruction() -> None:
    """JSON-object mode alone must not let the provider invent replacement keys."""
    output = {
        "claims": [{"claim": "observed", "evidence_ids": ["artifact:1"]}],
        "product_clusters": [],
        "opportunities": [],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(json.dumps(output))

    adapter = BailianModelAdapter(
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )

    adapter.generate_structured(_request(), AnalysisOutput)

    body = json.loads(seen[0].content)
    assert body["messages"][0]["role"] == "system"
    system_instruction = body["messages"][0]["content"]
    assert "Return exactly one JSON object" in system_instruction
    schema_text = system_instruction.split("JSON_SCHEMA:\n", 1)[1].split(
        "\nTASK_INSTRUCTIONS:\n", 1
    )[0]
    assert json.loads(schema_text) == AnalysisOutput.model_json_schema()
    assert "Additional properties are forbidden" in system_instruction
    assert system_instruction.endswith("TASK_INSTRUCTIONS:\nReturn JSON only.")
    assert body["messages"][1] == {
        "role": "user",
        "content": "Analyze persisted evidence.",
    }


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "```json\n{}\n```",
        "{}",
        '{"claims": [], "product_clusters": [], "opportunities": [], "extra": 1}',
    ],
)
def test_adapter_rejects_malformed_markdown_or_wrong_schema(content: str) -> None:
    adapter = BailianModelAdapter(
        api_key="secret",
        base_url="https://example.invalid/v1",
        model="deepseek-v3",
        transport=httpx.MockTransport(lambda _: _response(content)),
    )

    with pytest.raises(ModelOutputInvalid):
        adapter.generate_structured(_request(), AnalysisOutput)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "claims": [
                {"claim": "duplicate", "evidence_ids": ["artifact:1", "artifact:1"]}
            ],
            "product_clusters": [],
            "opportunities": [],
        },
        {
            "claims": [{"claim": "noncanonical", "evidence_ids": ["artifact:01"]}],
            "product_clusters": [],
            "opportunities": [],
        },
    ],
)
def test_output_citation_groups_are_unique_and_canonical(payload: dict[str, object]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalysisOutput.model_validate(payload)
