"""AIDLC-G reference implementation (milestones M0-M5 of the v0.3 compendium).

M0: C0-C3 contract schemas, canonical digests, lifecycle stages, policy matrix.
M1: authenticated gate service with Ed25519-signed CI attestations, protected
    manifest, append-only hash-chained audit ledger and fail-closed transitions.
M2: acceptance vertical slice - five agents, governed workspace, trusted CI
    runner executing real tests, orchestrator with human decision packets.
M3: coverage, review and docs agents, signed quality evidence (mutation, scans),
    Template I PR packet, protected main with gated merges.
M4: release evidence agent, signed build provenance, content-addressed artifacts,
    gated staging -> production canary deployments with automatic rollback.
M5: read-only operational support agent, Template K incident packets, human-only
    incident authority, seeded incident study with evidence ablations.
"""

__version__ = "0.8.0"
