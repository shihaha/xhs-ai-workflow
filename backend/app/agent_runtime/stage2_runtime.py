"""Compatibility alias for the canonical hardened Agent Runtime.

Stage 2 was accepted and folded into ``backend.app.agent_runtime.runtime``.
Importers that still reference ``Stage2AgentRuntime`` receive the exact same
class object; this module owns no runtime behavior.
"""

from backend.app.agent_runtime.runtime import AgentRuntime

Stage2AgentRuntime = AgentRuntime

__all__ = ["Stage2AgentRuntime"]
