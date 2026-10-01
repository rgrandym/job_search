"""Tool and agent registries.

A tool is an async function taking (args: <PydanticModel>, ctx: AgentContext). Its argument
model is the single source of truth for the JSON Schema the model sees and for validation.
An agent is a role prompt + the SKILL.md it follows + the tools it may call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from src.core.config import PROJECT_ROOT
from src.core.llm import Role, ToolSpec

if TYPE_CHECKING:
    from src.agents.runtime import AgentContext

A = TypeVar("A", bound=BaseModel)
ToolFn = Callable[[Any, "AgentContext"], Awaitable[Any]]

PROMPTS_DIR = Path(__file__).parent / "prompts"
SKILLS_DIR = PROJECT_ROOT / ".agent" / "skills"


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn

    @property
    def spec(self) -> ToolSpec:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, args: type[A]) -> Callable[[ToolFn], ToolFn]:
    """Register an async tool function."""

    def deco(fn: ToolFn) -> ToolFn:
        TOOLS[name] = Tool(name, description, args, fn)
        return fn

    return deco


@dataclass
class AgentDefinition:
    name: str
    description: str
    role: Role
    prompt_file: str
    allowed_tools: list[str]
    skill: str | None = None
    max_turns: int = 12
    _system: str | None = field(default=None, repr=False)

    def system_prompt(self) -> str:
        """Role prompt + the agent's skill as reference knowledge (cached after first build)."""
        if self._system is None:
            text = (PROMPTS_DIR / self.prompt_file).read_text(encoding="utf-8")
            if self.skill:
                skill = (SKILLS_DIR / self.skill / "SKILL.md").read_text(encoding="utf-8")
                skill = skill.split("---", 2)[-1].strip()  # drop YAML front matter
                text += (
                    f'\n\n<skill name="{self.skill}">\nReference knowledge. Where it shows '
                    "shell commands, use your tools instead; never ask the user to run "
                    f"commands.\n\n{skill}\n</skill>"
                )
            self._system = text
        return self._system

    def tool_specs(self) -> list[ToolSpec]:
        return [TOOLS[t].spec for t in self.allowed_tools]


AGENTS: dict[str, AgentDefinition] = {}


def register_agent(defn: AgentDefinition) -> AgentDefinition:
    AGENTS[defn.name] = defn
    return defn
