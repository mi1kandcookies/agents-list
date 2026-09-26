"""
specialists/contract_review/agent.py - the contract-review Specialist.

agent.yaml, prompts/, playbook/, rubrics/, tools.py (TOOL_DEFS) and
checks.py (CHECK_DEFS) beside this module do most of the work; the subclass
adds what is specific to contract review:

    extra_checks     every domain check is registered as "automated", so no
                     brief can turn a quote or redline check into a pending
                     one by giving it another kind
    validate_intake  the client's side must be customer or vendor (positions
                     differ by side); one contract per engagement (documents
                     it incorporates go under related_documents); v1 reads
                     .docx, .txt and .md contracts only (a PDF needs a text
                     version first)
    prepare          before the loop, pins under .agentkit/ (which the agent
                     cannot read or write) the contract the intake names and
                     the sha256 of every approved file the milestone builds
                     on (approved_inputs_unchanged params), so the checks can
                     tell the client's contract from other inputs and notice
                     an approved playbook or issue list rewritten later

Nothing here touches the human gate: human_review.required, the reviewer
role, the disclaimer and the attorney sign-off criteria all come from the
manifest, and every milestone ends pending the attorney's approval.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import MilestoneSpec, MissingInput
from specialists.contract_review.checks import CHECK_DEFS
from specialists.contract_review.tools import SIDES, record_baseline, record_engagement

READABLE_SUFFIXES = (".docx", ".txt", ".md")


def _side(value: Any) -> str | None:
    """customer or vendor when the answer names exactly one of them
    ("customer (buying)" counts), else None."""
    if not isinstance(value, str):
        return None
    named = set(re.findall(r"[a-z]+", value.lower())) & set(SIDES)
    return named.pop() if len(named) == 1 else None


def _file_names(value: Any) -> list[Any]:
    return [value] if isinstance(value, str) else list(value) if isinstance(value, list) else [value]


class ContractReview(Specialist):
    def extra_checks(self) -> list[dict[str, Any]]:
        return [{"name": name, "function": fn, "kind": "automated"} for name, fn in CHECK_DEFS.items()]

    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        gaps = {m.field for m in missing}
        if "side" not in gaps and _side(intake.get("side")) is None:
            missing.append(MissingInput(
                field="side", blocking=True,
                question="Which side are you on: customer (buying) or vendor (selling)? "
                         "Please answer with one of the two."))
        if "contract_files" not in gaps:
            files = _file_names(intake.get("contract_files"))
            unreadable = [str(f) for f in files
                          if not isinstance(f, str)
                          or PurePosixPath(f.replace("\\", "/")).suffix.lower() not in READABLE_SUFFIXES]
            if unreadable:
                missing.append(MissingInput(
                    field="contract_files", blocking=True,
                    question="These contracts cannot be read in v1: " + ", ".join(unreadable)
                             + ". Please upload each as .docx (preferred), .txt or .md."))
            elif len(files) > 1:
                missing.append(MissingInput(
                    field="contract_files", blocking=True,
                    question="Each engagement reviews and redlines one contract, and "
                             f"{len(files)} were named ({', '.join(map(str, files))}). Which one "
                             "should this engagement review? List order forms, DPAs, policies or "
                             "other documents it incorporates under related_documents (they are "
                             "read for context), and open a separate engagement for each other "
                             "contract you want redlined."))
        return missing

    def prepare(self, ctx: ToolContext, milestone: MilestoneSpec) -> None:
        super().prepare(ctx, milestone)
        intake = ctx.brief.intake if ctx.brief is not None else {}
        record_engagement(ctx.workspace, intake.get("contract_files"))
        for criterion in milestone.acceptance:
            if criterion.check == "approved_inputs_unchanged":
                record_baseline(ctx.workspace, [str(p) for p in criterion.params.get("paths") or []])
