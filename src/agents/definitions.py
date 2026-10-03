"""The assistant: one agent that turns what the user says into updates of their records.

Searching, screening, tailoring and cover letters run from the UI's buttons (services), not
through an agent. The job_matcher is not a chat agent: it is the screening model inside the
search pipeline (`src/jobs/screener.py`).
"""

from __future__ import annotations

from src.agents import tools as _tools  # noqa: F401  (registers tools)
from src.agents.registry import AgentDefinition, register_agent

ASSISTANT = register_agent(
    AgentDefinition(
        name="assistant",
        description="Updates the career intent, search preferences and proposed CV facts.",
        role="quality",
        prompt_file="assistant.md",
        allowed_tools=[
            "get_context",
            "update_search_intent",
            "update_preferences",
            "propose_cv_facts",
        ],
        max_turns=6,
    )
)
