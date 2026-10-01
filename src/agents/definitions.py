"""The agent roster: one orchestrator, three specialists.

job_matcher is a subagent without a chat loop: it is invoked inside `search_jobs` to judge
the shortlist in parallel batches (see `src/jobs/screener.py`). It is listed here so the UI
and docs show the full team.
"""

from __future__ import annotations

from src.agents import tools as _tools  # noqa: F401  (registers tools)
from src.agents.registry import AgentDefinition, register_agent

ORCHESTRATOR = register_agent(
    AgentDefinition(
        name="orchestrator",
        description="Understands the request, summarises the profile, delegates, reports back.",
        role="orchestrator",
        prompt_file="orchestrator.md",
        allowed_tools=["get_workspace_state", "summarize_profile", "delegate"],
        max_turns=10,
    )
)

JOB_SEARCH_EXPERT = register_agent(
    AgentDefinition(
        name="job_search_expert",
        description="Runs searches, refines strategy (titles, radius), reports true matches.",
        role="worker",
        prompt_file="job_search_expert.md",
        skill="job_search",
        allowed_tools=["search_jobs", "get_results", "get_job", "list_sources"],
        max_turns=8,
    )
)

CV_EXPERT = register_agent(
    AgentDefinition(
        name="cv_expert",
        description="Tailors the CV to a chosen job without inventing facts and exports .docx.",
        role="worker",
        prompt_file="cv_expert.md",
        skill="cv_writer",
        allowed_tools=["get_master_cv", "tailor_cv", "update_master_cv", "get_job"],
        max_turns=6,
    )
)

JOB_MATCHER_INFO = {
    "name": "job_matcher",
    "description": "Judges each shortlisted posting against the profile summary (fit 0-100, "
    "reasons, gaps, dealbreakers). Runs inside search_jobs.",
    "role": "worker",
}
