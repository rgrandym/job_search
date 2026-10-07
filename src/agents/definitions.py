"""The assistant: one agent that operates the workspace through existing services."""

from __future__ import annotations

from src.agents import actions as _actions  # noqa: F401  (registers tools)
from src.agents import actions_more as _actions_more  # noqa: F401  (registers tools)
from src.agents import tools as _tools  # noqa: F401  (registers tools)
from src.agents.registry import AgentDefinition, register_agent

ASSISTANT = register_agent(
    AgentDefinition(
        name="assistant",
        description="Operates the job-search workspace and updates the user's records.",
        role="quality",
        prompt_file="assistant.md",
        allowed_tools=[
            "get_context",
            "update_profile",
            "update_search_intent",
            "update_preferences",
            "propose_cv_facts",
            "search_jobs",
            "set_search_filters",
            "find_jobs",
            "save_jobs",
            "remove_saved_jobs",
            "track_job",
            "label_job",
            "tailor_cv",
            "write_cover_letter",
            "select_cv",
            "refresh_profile",
            "export_general_cv",
            "review_workspace",
            "decide_cv_fact",
            "open_search_history",
            "continue_search",
            "recheck_postings",
            "suggest_learning",
            "decide_learning",
            "add_company",
            "remove_company",
            "discover_companies",
            "edit_cv_basics",
            "edit_cv_role",
            "record_application",
            "edit_tracker_entry",
            "list_documents",
            "edit_tailored_cv",
            "edit_cover_letter",
            "build_profile",
            "delete_cv",
            "delete_profile",
            "delete_search_history",
            "remove_search_result",
            "delete_tracker_entry",
            "delete_cover_letter",
            "remove_learned_preference",
            "change_app_models",
        ],
        max_turns=12,
    )
)
