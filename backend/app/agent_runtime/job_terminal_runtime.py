"""Compatibility import for the folded Job-bound terminal Runtime.

Terminal projection now belongs to ``JobBoundAgentRuntime`` itself. Keep this
module only so stacked callers/tests using the experimental class name do not
break while the branch chain is folded forward.
"""

from backend.app.agent_runtime.job_bound_runtime import JobBoundAgentRuntime


TerminalProjectingJobBoundAgentRuntime = JobBoundAgentRuntime

__all__ = ["TerminalProjectingJobBoundAgentRuntime"]
