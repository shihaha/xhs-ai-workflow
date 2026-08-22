from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from backend.app.agent_runtime.context import RuntimeContext
from backend.app.agent_runtime.model import PydanticDecisionModel


def test_pydantic_decision_model_returns_project_owned_structured_action() -> None:
    def decide(_messages, info):
        assert info.output_tools
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "action": "finish",
                        "final_output": {"ok": True},
                    },
                )
            ]
        )

    driver = PydanticDecisionModel(FunctionModel(decide))
    context = RuntimeContext(
        goal="finish deterministically",
        run_id="run-test",
        usage={"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
        remaining_budget={"steps": 5, "model_calls": 2, "input_tokens": 1000, "output_tokens": 1000, "wall_time_seconds": 5.0},
        tools=[],
        recent_steps=[],
    )

    turn = driver.next_action(context)

    assert turn.action.action == "finish"
    assert turn.action.final_output == {"ok": True}
    assert turn.model_name is not None
