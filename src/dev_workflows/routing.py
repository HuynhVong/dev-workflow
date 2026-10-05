"""Which model and which Claude Code skills each workflow step uses.

Every AI step names itself (`step="plan_implementation"`). `Routing` turns that name into:
- a model: a tier (haiku / sonnet / opus) from STEPS, overridable per step in workspace.yaml;
- skills: the step's preferred skills plus domain and repo-stack skills, picked at run time from the skills the
  developer installed globally for Claude Code (~/.claude/skills and plugin skills). Missing skills are skipped.

Effort is fixed at medium for every model (Huynh, 2026-10-05). Steps not listed here use no AI.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

EFFORT = "medium"

MODELS = {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# Haiku 4.5 does not take the effort parameter; it always runs at its single built-in level.
NO_EFFORT_PARAM = ("claude-haiku-4",)


@dataclass(frozen=True)
class Step:
    model: str                      # tier name or a full model id
    skills: tuple[str, ...] = ()    # preferred global skills, in order
    domain: bool = False            # also add the workspace's domain skills
    stack: bool = False             # also add skills matching the repo's stack (coding steps)
    escalate_to: str = ""           # model for the final automated fix attempt


# Defaults follow Huynh's table. Where the table offers two models ("Sonnet / Opus"), the first is the default and
# the second is the escalation (coding) or a per-step override in workspace.yaml.
STEPS: dict[str, Step] = {
    # jira ticket implement
    "gather_context": Step("haiku", ("confluence-read",)),
    "analyze_requirements": Step("sonnet", ("requirements-analysis",)),
    "change_impact": Step("sonnet", ("repo-exploration",)),
    "discover_repos": Step("haiku", ("repo-search",)),
    "plan_implementation": Step("opus", ("planning",), domain=True),
    "implement": Step("sonnet", (), stack=True, escalate_to="opus"),
    "targeted_fix": Step("sonnet", (), stack=True, escalate_to="opus"),
    "analyze_feedback_and_route": Step("sonnet", ("debugging",), domain=True),
    "integration_check": Step("sonnet", ("playwright",)),
    "repo_review": Step("sonnet", ("code-review", "sonar")),
    "contract_review": Step("opus", ("cross-repo-review",)),
    "summary": Step("haiku", ("implementation-summary",)),
    # address-review
    "classify_comments": Step("sonnet", ("code-review",)),
    "map_to_repos": Step("sonnet", ("planning",)),
    # ticket review (code review itself runs as the pr_review.* steps below)
    "ticket_review.understand": Step("sonnet", ("requirements-analysis", "confluence-read")),
    "ticket_review.coverage": Step("sonnet", ("code-review",)),
    "ticket_review.test_plan": Step("opus", ("planning", "playwright"), domain=True),
    "ticket_review.e2e": Step("sonnet", ("playwright",)),
    "ticket_review.comment": Step("haiku", ("implementation-summary",)),
    # simple workflows
    "ticket_to_plan.analyze": Step("sonnet", ("requirements-analysis",)),
    "ticket_to_plan.plan": Step("opus", ("planning",), domain=True),
    "ticket_to_plan.critique": Step("sonnet", ("planning",)),
    "pr_review.triage": Step("haiku", ("code-review",)),
    "pr_review.lens": Step("sonnet", ("code-review",)),
    "pr_review.verdict": Step("sonnet", ("code-review",)),
    "standup": Step("haiku", ("standup",)),
}

# Files that reveal a repo's stack, for picking repo-specific coding skills.
STACK_MARKERS = {
    "package.json": ("javascript", "typescript", "node"), "tsconfig.json": ("typescript",), "angular.json": ("angular",),
    "next.config.js": ("nextjs", "react"), "vite.config.ts": ("vite", "react", "vue"), "pom.xml": ("java", "spring", "maven"),
    "build.gradle": ("java", "kotlin", "spring", "gradle"), "build.gradle.kts": ("kotlin", "gradle"), "go.mod": ("go", "golang"),
    "pyproject.toml": ("python",), "requirements.txt": ("python",), "Gemfile": ("ruby", "rails"), "composer.json": ("php", "laravel"),
    "Cargo.toml": ("rust",), "pubspec.yaml": ("flutter", "dart"), "*.csproj": ("dotnet", "csharp"),
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str

    def body(self, limit: int = 20000) -> str:
        text = Path(self.path).read_text(errors="replace")
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
        return text if len(text) <= limit else text[:limit] + "\n…(truncated)"


def default_skill_dirs() -> list[Path]:
    home = Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    return [home / "skills", home / "plugins"]


class SkillRegistry:
    """The skills installed globally for Claude Code: <dir>/<name>/SKILL.md, including skills inside plugins."""

    def __init__(self, dirs: list[Path] | None = None):
        self.dirs = [Path(d).expanduser() for d in (dirs if dirs is not None else default_skill_dirs())]
        self._skills: dict[str, Skill] | None = None

    @property
    def skills(self) -> dict[str, Skill]:
        if self._skills is None:
            found: dict[str, Skill] = {}
            for d in self.dirs:
                if d.is_dir():
                    for f in sorted(d.rglob("SKILL.md")):
                        s = _parse(f)
                        found.setdefault(s.name, s)
            self._skills = found
        return self._skills

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name) or next((s for n, s in self.skills.items() if n.split(":")[-1] == name), None)

    def matching(self, words: list[str], limit: int = 3) -> list[Skill]:
        """Skills whose name or description mentions any of `words` (e.g. a repo's stack), best match first."""
        scored = []
        for s in self.skills.values():
            hay = f"{s.name} {s.description}".lower()
            score = sum(1 for w in words if re.search(rf"\b{re.escape(w.lower())}\b", hay))
            if score:
                scored.append((-score, s.name, s))
        return [s for _, _, s in sorted(scored)[:limit]]


def _parse(path: Path) -> Skill:
    text = path.read_text(errors="replace")
    meta = {}
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip("'\"")
    return Skill(name=meta.get("name") or path.parent.name, description=meta.get("description", ""), path=str(path))


def repo_stack(path: str) -> list[str]:
    words: list[str] = []
    for marker, stack in STACK_MARKERS.items():
        hit = any(Path(path).glob(marker)) if "*" in marker else Path(path, marker).exists()
        if hit:
            words += [w for w in stack if w not in words]
    return words


@dataclass
class Routing:
    """Resolves a step to (model, effort, skills). `overrides` come from workspace.yaml:
    models: {haiku: ..., sonnet: ..., opus: ...}
    steps: {plan_implementation: {model: sonnet, skills: [planning, my-team-planning]}}
    domain_skills: [insurance-domain]"""
    registry: SkillRegistry = field(default_factory=SkillRegistry)
    models: dict[str, str] = field(default_factory=lambda: dict(MODELS))
    steps: dict[str, Step] = field(default_factory=lambda: dict(STEPS))
    domain_skills: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, raw: dict | None, registry: SkillRegistry | None = None) -> "Routing":
        raw = raw or {}
        steps = dict(STEPS)
        for name, o in (raw.get("steps") or {}).items():
            base = steps.get(name, Step("sonnet"))
            steps[name] = Step(model=o.get("model", base.model), skills=tuple(o.get("skills", base.skills)),
                               domain=o.get("domain", base.domain), stack=o.get("stack", base.stack),
                               escalate_to=o.get("escalate_to", base.escalate_to))
        return cls(registry=registry or SkillRegistry(raw.get("skill_dirs")), models={**MODELS, **(raw.get("models") or {})},
                   steps=steps, domain_skills=tuple(raw.get("domain_skills") or ()))

    def step(self, name: str) -> Step:
        if name not in self.steps:
            raise KeyError(f"no model routing for step {name!r}; add it to routing.STEPS")
        return self.steps[name]

    def model(self, name: str, escalate: bool = False) -> str:
        s = self.step(name)
        tier = s.escalate_to if escalate and s.escalate_to else s.model
        return self.models.get(tier, tier)

    @staticmethod
    def effort(model: str) -> str | None:
        return None if model.startswith(NO_EFFORT_PARAM) else EFFORT

    def skills(self, name: str, repo_path: str | None = None) -> list[Skill]:
        s = self.step(name)
        wanted = list(s.skills) + (list(self.domain_skills) if s.domain else [])
        out = [k for k in (self.registry.get(n) for n in wanted) if k]
        if s.stack and repo_path:
            out += [k for k in self.registry.matching(repo_stack(repo_path)) if k not in out]
        return out

    def missing(self) -> dict[str, list[str]]:
        """Preferred skills that are not installed, per step (for `devflow setup` and preflight)."""
        out = {}
        for name, s in self.steps.items():
            gone = [n for n in list(s.skills) + (list(self.domain_skills) if s.domain else []) if not self.registry.get(n)]
            if gone:
                out[name] = gone
        return out


def setup_report(routing: "Routing", repo_paths: dict[str, str] | None = None) -> tuple[str, bool]:
    """`devflow setup`: the model, effort and skills of every AI step, and which skills still need installing.
    Returns (report, all_installed)."""
    lines = [f"Skills folders: {', '.join(str(d) for d in routing.registry.dirs)}",
             f"Installed skills: {', '.join(sorted(routing.registry.skills)) or '(none)'}", "",
             f"{'step':<28} {'model':<28} {'effort':<8} skills (✓ installed, ✗ missing)"]
    missing = routing.missing()
    for name, s in routing.steps.items():
        model = routing.model(name)
        wanted = list(s.skills) + (list(routing.domain_skills) if s.domain else [])
        marks = [f"{'✗' if n in missing.get(name, []) else '✓'} {n}" for n in wanted]
        if s.stack:
            marks.append("repo-stack coding skills")
        if s.escalate_to:
            marks.append(f"last fix attempt on {routing.model(name, escalate=True)}")
        marks = ", ".join(marks) or "-"
        lines.append(f"{name:<28} {model:<28} {routing.effort(routing.model(name)) or 'n/a':<8} {marks}")
    for repo, path in (repo_paths or {}).items():
        stack = repo_stack(path)
        found = [k.name for k in routing.registry.matching(stack)]
        lines.append(f"\n{repo}: stack {stack or '(not detected)'} -> coding skills {found or '(none installed; add one for this stack)'}")
    gone = sorted({n for m in missing.values() for n in m})
    if gone:
        lines += ["", "Install these skills globally for Claude Code (each as <skills folder>/<name>/SKILL.md), or point the "
                      "steps at the skills you have under `ai.steps` in workspace.yaml:", *[f"  - {n}" for n in gone]]
    else:
        lines += ["", "All preferred skills are installed."]
    return "\n".join(lines), not gone


def skills_system_block(skills: list[Skill]) -> str:
    """Skill instructions appended to an API call's system prompt (the API cannot load local skills itself)."""
    if not skills:
        return ""
    return "\n\n" + "\n\n".join(f"<skill name='{s.name}'>\n{s.body()}\n</skill>" for s in skills) + \
        "\n\nFollow the skills above where they apply to this task."


def skills_hint(skills: list[Skill]) -> str:
    """For Claude Code steps: it loads global skills itself; this names the ones that fit this step."""
    if not skills:
        return ""
    return ("\n\nSkills installed for this kind of work (use the Skill tool to load the ones that apply before you start):\n"
            + "\n".join(f"- {s.name}: {s.description}" for s in skills))
