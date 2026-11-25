"""The preserved eight AIDLC stages and the evidence each exit gate needs.

RELEASE_READY is the post-stage-8 release state (section 6.9), not a ninth
lifecycle stage.
"""

from enum import Enum
from typing import Dict, Optional, Tuple


class Stage(str, Enum):
    INITIATIVE = "INITIATIVE"
    EPIC = "EPIC"
    SPIKE = "SPIKE"
    STORIES = "STORIES"
    ACCEPTANCE_TESTS = "ACCEPTANCE_TESTS"
    IMPLEMENTATION = "IMPLEMENTATION"
    TEST_COVERAGE = "TEST_COVERAGE"
    DOCUMENTATION = "DOCUMENTATION"
    RELEASE_READY = "RELEASE_READY"


STAGE_ORDER = list(Stage)

GOVERNANCE_LAYER = {
    Stage.INITIATIVE: "Prompt Governance",
    Stage.EPIC: "Prompt Governance",
    Stage.SPIKE: "Prompt Governance",
    Stage.STORIES: "Prompt Governance",
    Stage.ACCEPTANCE_TESTS: "Output Validation",
    Stage.IMPLEMENTATION: "Output Validation",
    Stage.TEST_COVERAGE: "Output Validation",
    Stage.DOCUMENTATION: "Behavioral Monitoring",
    Stage.RELEASE_READY: "Behavioral Monitoring",
}

CONTRACT_SLOTS = ("C0", "C1", "C2", "STORIES", "C3", "DOCS", "RELEASE")
BUILD_SLOTS = ("BASELINE", "BUILD")
ALL_SLOTS = CONTRACT_SLOTS + BUILD_SLOTS

# Evidence is cumulative: entering a stage re-checks everything upstream, so a
# stale C1 blocks a release even after the work item has moved past the epic.
_GATE_SLOTS: Dict[Stage, Tuple[str, ...]] = {
    Stage.EPIC: ("C0",),
    Stage.SPIKE: ("C0", "C1"),
    Stage.STORIES: ("C0", "C1", "C2"),
    Stage.ACCEPTANCE_TESTS: ("C0", "C1", "C2", "STORIES"),
    Stage.IMPLEMENTATION: ("C0", "C1", "C2", "STORIES", "C3", "BASELINE"),
    Stage.TEST_COVERAGE: ("C0", "C1", "C2", "STORIES", "C3", "BUILD"),
    Stage.DOCUMENTATION: ("C0", "C1", "C2", "STORIES", "C3", "BUILD"),
    Stage.RELEASE_READY: ("C0", "C1", "C2", "STORIES", "C3", "BUILD", "DOCS", "RELEASE"),
}


def next_stage(stage: Stage) -> Optional[Stage]:
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


def gate_slots(target: Stage) -> Tuple[str, ...]:
    return _GATE_SLOTS[target]


def requires_review(target: Stage) -> bool:
    return STAGE_ORDER.index(target) >= STAGE_ORDER.index(Stage.DOCUMENTATION)
