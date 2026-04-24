"""Alignment synthesizer orchestrator.

Bridges the LLM agent (researchos/agent_defs/alignment_synthesizer.md) to
the pipeline. Loads inputs, computes provenance hashes, dispatches the
agent (via injected callable), and validates the produced contract.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Protocol

import yaml

from researchos.schemas.alignment_contract import AlignmentContract

logger = logging.getLogger(__name__)


class DispatchFn(Protocol):
    """LLM dispatcher signature. Returns nothing; the agent writes the file."""

    def __call__(
        self, *, pipeline_dir: Path, agent_name: str, prompt_extras: str
    ) -> None: ...


def _stub_dispatch(*, pipeline_dir: Path, agent_name: str, prompt_extras: str) -> None:
    raise NotImplementedError(
        "alignment_synthesizer: dispatch_fn must be wired by the scheduler. "
        "Pass a real dispatcher via run_synthesizer(..., dispatch_fn=...)."
    )


def file_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of the file at *path*."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_synthesizer(
    pipeline_dir: Path,
    *,
    dispatch_fn: DispatchFn = _stub_dispatch,  # type: ignore[assignment]
) -> Path:
    """Run the alignment_synthesizer agent on a pipeline directory.

    Loads paper_spec.yaml + proposal_spec.yaml + (optional) code_recon.yaml,
    dispatches the LLM agent, validates the produced contract, and returns
    the contract path.

    Parameters
    ----------
    pipeline_dir:
        The pipeline artifact directory (e.g. ``artifacts/pipeline_abc123/``).
    dispatch_fn:
        Callable that invokes the LLM agent.  Signature matches
        ``DispatchFn``.  Defaults to a stub that raises ``NotImplementedError``;
        inject a real dispatcher (or a test fake) before calling in production.

    Returns
    -------
    Path
        Absolute path to the written ``alignment_contract.yaml``.

    Raises
    ------
    FileNotFoundError
        If ``paper_spec.yaml`` or ``proposal_spec.yaml`` are absent.
    RuntimeError
        If the agent fails to produce a contract, or if the contract's
        provenance hashes do not match the inputs that were read.
    pydantic.ValidationError
        If the contract does not satisfy ``AlignmentContract``.
    """
    pdir = Path(pipeline_dir)
    paper = pdir / "paper_spec.yaml"
    proposal = pdir / "proposal_spec.yaml"
    code_recon = pdir / "code_recon.yaml"
    contract_path = pdir / "alignment_contract.yaml"

    # ------------------------------------------------------------------ #
    # 1. Verify required inputs are present                               #
    # ------------------------------------------------------------------ #
    if not paper.exists():
        raise FileNotFoundError(f"missing paper_spec.yaml in {pdir}")
    if not proposal.exists():
        raise FileNotFoundError(f"missing proposal_spec.yaml in {pdir}")

    # ------------------------------------------------------------------ #
    # 2. Compute provenance hashes                                        #
    # ------------------------------------------------------------------ #
    expected_hashes: dict[str, str | None] = {
        "paper_spec_hash": file_sha256(paper),
        "proposal_spec_hash": file_sha256(proposal),
        "code_recon_hash": file_sha256(code_recon) if code_recon.exists() else None,
    }

    if not code_recon.exists():
        logger.warning(
            "alignment_synthesizer: code_recon.yaml not found in %s — "
            "proceeding without it (code_recon_hash will be null).",
            pdir,
        )

    # ------------------------------------------------------------------ #
    # 3. Build prompt_extras with hashes for the agent to copy verbatim  #
    # ------------------------------------------------------------------ #
    # Providing hashes to the agent avoids drift if it were to tamper
    # with input files between reading and writing the contract.
    prompt_extras = (
        "## Provenance hashes (copy into contract.sources verbatim)\n"
        f"paper_spec_hash: {expected_hashes['paper_spec_hash']}\n"
        f"proposal_spec_hash: {expected_hashes['proposal_spec_hash']}\n"
        f"code_recon_hash: {expected_hashes['code_recon_hash']}\n"
    )

    # ------------------------------------------------------------------ #
    # 4. Dispatch the LLM agent                                           #
    # TODO(scheduler integration): wire a real dispatcher here.           #
    # The scheduler should call:                                           #
    #   from researchos.runtime.dispatcher import dispatch_agent          #
    #   run_synthesizer(pdir, dispatch_fn=dispatch_agent)                 #
    # ------------------------------------------------------------------ #
    dispatch_fn(
        pipeline_dir=pdir,
        agent_name="alignment_synthesizer",
        prompt_extras=prompt_extras,
    )

    # ------------------------------------------------------------------ #
    # 5. Verify the agent produced the contract file                      #
    # ------------------------------------------------------------------ #
    if not contract_path.exists():
        raise RuntimeError(
            f"alignment_synthesizer did not produce {contract_path}"
        )

    # ------------------------------------------------------------------ #
    # 6. Parse and validate against AlignmentContract pydantic schema    #
    # ------------------------------------------------------------------ #
    contract_data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract = AlignmentContract.model_validate(contract_data)

    # ------------------------------------------------------------------ #
    # 7. Verify provenance hashes match                                   #
    # This forces the agent to have read the actual current versions,     #
    # not stale cached copies.                                            #
    # ------------------------------------------------------------------ #
    if contract.sources.paper_spec_hash != expected_hashes["paper_spec_hash"]:
        raise RuntimeError(
            f"contract.sources.paper_spec_hash mismatch — agent may have "
            f"used a stale input. Expected {expected_hashes['paper_spec_hash']!r}, "
            f"got {contract.sources.paper_spec_hash!r}."
        )
    if contract.sources.proposal_spec_hash != expected_hashes["proposal_spec_hash"]:
        raise RuntimeError(
            "contract.sources.proposal_spec_hash mismatch — agent may have "
            "used a stale input. "
            f"Expected {expected_hashes['proposal_spec_hash']!r}, "
            f"got {contract.sources.proposal_spec_hash!r}."
        )
    if (
        expected_hashes["code_recon_hash"] is not None
        and contract.sources.code_recon_hash != expected_hashes["code_recon_hash"]
    ):
        raise RuntimeError(
            "contract.sources.code_recon_hash mismatch — agent may have "
            "used a stale input. "
            f"Expected {expected_hashes['code_recon_hash']!r}, "
            f"got {contract.sources.code_recon_hash!r}."
        )

    logger.info(
        "alignment_synthesizer: produced contract with status=%s, "
        "open_tensions=%d",
        contract.contract_status,
        len(contract.open_tensions),
    )
    return contract_path


__all__ = [
    "DispatchFn",
    "file_sha256",
    "run_synthesizer",
]
