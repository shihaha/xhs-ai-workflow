"""Versioned production guidance adapted from the tutorial, without demo records."""

from typing import Final


TEMPLATE_SEEDS: Final[dict[str, dict[str, object]]] = {
    "list-v1": {
        "version": "tutorial-list-v1",
        "structure": ["audience", "step_a", "step_b", "step_c", "boundaries"],
        "rules": [
            "Each step states a concrete action.",
            "Title and body address the same task.",
            "Use only explicitly cited product evidence.",
        ],
    },
    "problem-solution-v1": {
        "version": "tutorial-problem-solution-v1",
        "structure": ["problem_context", "cause", "actions", "boundaries"],
        "rules": [
            "Address exactly one problem.",
            "Make the proposed actions executable.",
            "Use only explicitly cited product evidence.",
        ],
    },
}
