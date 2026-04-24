"""Platform-agnostic core of ResearchOS.

This package contains all pipeline logic — scheduling, dispatching,
contract enforcement, preflight, reverify — that does NOT depend on
any specific agent host.

Files are progressively migrated here from researchos.runtime during
the 2026-04 refactor. Until migration completes, both locations coexist;
new modules (contract_guard, etc.) are placed here directly.
"""
