"""Product packs: point the framework at a product of your own.

A product pack is a directory:

    my-product/
      product.json   what the sponsor wants, the change to make, and the team's stack (see TEMPLATE)
      instructions/  the team's instruction files for the agents (coding standards, framework conventions)
      repo/          the code the agents start from (or set "repo" to an existing checkout)

The stack is the team's choice: a built-in id (`scripts/product.py stacks` lists
them) or a full profile object for anything else. The engineering lead confirms
it during the run, before design.

You do not write the domain scenarios, answers or approvals in the pack. The
terminal desk asks you for them while the run is going, which is the point:
people decide the behavior, and agents only translate it.

Limits of the simulation: JVM and .NET stacks need their build tool (mvn,
gradle, dotnet) installed where the runner runs, and mutation testing runs only
for Python. Deployments go to simulated environments,
and their telemetry is nominal: each release health check is reported exactly
at its threshold. A real rollout replaces the deployer and telemetry with
adapters for your runtime platform.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .deploy import Telemetry
from .errors import GovernanceError
from .contracts import StackProfile
from .orchestrator import Engineering, ProductRequest, SliceEnvironment, product_ids
from .stacks import BUILT_IN, DEFAULT_STACK, read_instructions

PLACEHOLDER = "TODO"
SKIP_DIRS = {".git", ".hg", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", "dist", "build", ".idea", ".vscode"}
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 500


class SponsorGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    measurement_source: str = Field(min_length=1)
    baseline: Optional[str] = None
    target: Optional[str] = None


class PackFile(BaseModel):
    """The contents of product.json."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}$", description="Short id used in artifact ids, e.g. INVOICE")
    name: str = Field(min_length=1)
    sponsor: SponsorGoal
    change_request: str = Field(min_length=1)
    repo: str = "repo"
    artifact_name: Optional[str] = None
    stack: Union[str, StackProfile] = DEFAULT_STACK.stack_id
    instructions: str = "instructions"

    @field_validator("stack")
    @classmethod
    def _known_stack(cls, value: Union[str, StackProfile]) -> Union[str, StackProfile]:
        if isinstance(value, str) and value not in BUILT_IN:
            raise ValueError(f"unknown stack {value!r}; built-in stacks: {sorted(BUILT_IN)}")
        return value

    @field_validator("change_request", "name")
    @classmethod
    def _filled(cls, value: str) -> str:
        if PLACEHOLDER in value:
            raise ValueError(f"still contains the {PLACEHOLDER} placeholder")
        return value


@dataclass
class ProductPack:
    key: str
    name: str
    seed: Dict[str, bytes]
    request: ProductRequest
    source: Path
    engineering: Engineering = field(default_factory=Engineering)

    @property
    def work_item_id(self) -> str:
        return f"WI-{self.key}-001"


def read_seed(root: Path) -> Dict[str, bytes]:
    """Every file under root, keyed by its posix path, skipping tool and build directories."""
    if not root.is_dir():
        raise ValueError(f"repo directory {root} does not exist")
    seed: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        seed[rel.as_posix()] = path.read_bytes()
        if len(seed) > MAX_FILES:
            raise ValueError(f"{root} has more than {MAX_FILES} files; point repo at the part of the code this change touches")
    if not seed:
        raise ValueError(f"repo directory {root} is empty")
    return seed


def _unfilled(data: Any, where: str = "") -> Optional[str]:
    if isinstance(data, dict):
        return next((hit for k, v in data.items() if (hit := _unfilled(v, f"{where}.{k}" if where else k))), None)
    if isinstance(data, str) and PLACEHOLDER in data:
        return where
    return None


def load_pack(directory: Path) -> ProductPack:
    directory = Path(directory)
    spec_path = directory / "product.json"
    if not spec_path.is_file():
        raise ValueError(f"{spec_path} not found; create a pack with: scripts/product.py init {directory}")
    raw = json.loads(spec_path.read_text())
    hole = _unfilled(raw)
    if hole:
        raise ValueError(f"product.json field '{hole}' still contains {PLACEHOLDER}; fill it in first")
    spec = PackFile.model_validate(raw)
    repo = Path(spec.repo)
    seed = read_seed(repo if repo.is_absolute() else directory / repo)
    ids = product_ids(spec.key)
    sponsor_request = {
        "initiative_id": ids["C0"], "key_result_id": f"KR-{spec.key}-1", "metric_owner": "sponsor",
        **spec.sponsor.model_dump(),
    }
    request = ProductRequest(sponsor_request=sponsor_request, change_request=spec.change_request, ids=ids,
                             artifact_name=spec.artifact_name or f"{spec.key.lower()}.tar")
    stack = BUILT_IN[spec.stack] if isinstance(spec.stack, str) else spec.stack
    instructions_dir = Path(spec.instructions)
    instructions = read_instructions(instructions_dir if instructions_dir.is_absolute() else directory / instructions_dir)
    options = {**BUILT_IN, stack.stack_id: stack}
    return ProductPack(spec.key, spec.name, seed, request, directory, Engineering(stack, instructions, options))


TEMPLATE = {
    "key": "MYPRODUCT",
    "name": "TODO: the product or feature, in a few words",
    "sponsor": {
        "problem": "TODO: what users or the business struggle with today",
        "metric": "TODO: the number that shows the problem is solved",
        "measurement_source": "TODO: where that number comes from (analytics, support tickets, logs)",
        "baseline": None,
        "target": None,
    },
    "change_request": "TODO: the change you want, in plain words. Behavior, not implementation.",
    "stack": "python-pytest",
    "repo": "repo",
    "instructions": "instructions",
}

INSTRUCTIONS_TEMPLATE = """# Team conventions

Every agent reads this file before it designs, tests, writes or reviews code.
Write what your team would tell a new engineer. For example:

- Framework and versions: e.g. Spring Boot 3.3 on Java 21, or Micronaut 4, or ASP.NET Core 8.
- Project layout and module names, and which package is the public domain interface.
- Coding standards: naming, error handling, logging, null handling.
- Libraries you use (and ones you do not), e.g. MapStruct, Lombok, FluentValidation.
- Testing style: JUnit 5 and AssertJ, or xUnit and FluentAssertions; how fixtures are built.

These files set conventions. They cannot grant an agent permission to skip a gate.
"""


def scaffold(directory: Path, key: str = "MYPRODUCT", stack: str = DEFAULT_STACK.stack_id) -> Path:
    """Create a pack to fill in. Refuses to overwrite an existing product.json."""
    directory = Path(directory)
    if (directory / "product.json").exists():
        raise ValueError(f"{directory / 'product.json'} already exists")
    if stack not in BUILT_IN:
        raise ValueError(f"unknown stack {stack!r}; built-in stacks: {sorted(BUILT_IN)}")
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", key):
        raise ValueError("key must be 2-16 capital letters or digits, starting with a letter, e.g. INVOICE")
    package = key.lower()
    (directory / "repo" / "src" / package).mkdir(parents=True, exist_ok=True)
    (directory / "product.json").write_text(json.dumps({**TEMPLATE, "key": key, "stack": stack}, indent=2) + "\n")
    (directory / "instructions").mkdir(exist_ok=True)
    (directory / "instructions" / "conventions.md").write_text(INSTRUCTIONS_TEMPLATE)
    (directory / "repo" / "README.md").write_text(
        f"# {key.title()}\n\nDescribe what the product does today. The requirements agent reads this "
        "(as untrusted data) to learn the current behavior.\n")
    if BUILT_IN[stack].language == "python":
        (directory / "repo" / "src" / package / "__init__.py").write_text(
            f'"""{key.title()}: replace with the existing code, or leave empty for a new product."""\n')
    else:
        (directory / "repo" / "src" / package).rmdir()
        (directory / "repo" / "src" / "README.md").write_text(
            f"Put the existing {BUILT_IN[stack].framework} code here, with the build file at the repo root. Build rules:\n\n"
            + "".join(f"- {n}\n" for n in BUILT_IN[stack].build_notes))
    return directory


def nominal_telemetry(env: SliceEnvironment, release_id: str) -> Telemetry:
    """Simulated runtime: report every health check of the approved release exactly at its threshold."""
    def telemetry(environment: str, artifact_digest: str, phase: str) -> Dict[str, int]:
        try:
            release = env.gate.get_artifact(env.admin, release_id)
        except GovernanceError:  # no release yet: nothing to report
            return {}
        return {c["metric"]: c["threshold"] for c in release["body"].get("health_checks", [])}
    return telemetry


def environment_for(pack: ProductPack, advisories: Optional[Dict[Any, str]] = None,
                    telemetry: Optional[Callable[[SliceEnvironment], Telemetry]] = None) -> SliceEnvironment:
    """A fresh simulated environment seeded with the pack's repo."""
    holder: Dict[str, Telemetry] = {}
    env = SliceEnvironment.create(pack.seed, advisories, lambda e, d, p: holder["t"](e, d, p), pack.engineering.stack)
    holder["t"] = (telemetry or (lambda e: nominal_telemetry(e, pack.request.ids["RELEASE"])))(env)
    return env
