import json

import httpx
import pytest

from backend.app.adapters.bailian import (
    BailianModelAdapter,
    ModelOutputInvalid,
    StructuredModelRequest,
)
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
