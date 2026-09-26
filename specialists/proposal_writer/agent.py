"""
specialists/proposal_writer/agent.py - the proposal-writer Specialist.

agent.yaml, tools.py (TOOL_DEFS) and checks.py (CHECK_DEFS) beside this
module carry the domain; the subclass adds what is specific to proposals:

    extra_checks        the domain checks, registered as automated so that no
                        brief can turn one into a pending rubric or human check
    validate_intake     engagement_mode must be "rfp" or "questionnaire"
    propose_milestones  a questionnaire engagement also delivers its answer
                        sheet in m3-draft
    system_prompt       the citation rule the domain checks enforce: company
                        facts cite [KB:...] passages; claim-ledger ids ([C#])
                        may sit beside them but never replace them
    finalize            an answer sheet left in m3-draft is hashed with the
                        submission, since questionnaire_answers_grounded
                        grades it

Nothing here relaxes a check or the human gate: the compliance matrix, the
evidence map and the answer sheet are checked exactly as the agent left them.
"""
from __future__ import annotations

from typing import Any, Mapping

from agentkit.loop import RunOutcome
from agentkit.specialist import Specialist
from agentkit.tools import ToolContext
from agentkit.types import Brief, MilestoneSpec, MissingInput
from specialists.proposal_writer.tools import ANSWERS_PATH

ENGAGEMENT_MODES = ("rfp", "questionnaire")
DRAFT_MILESTONE = "m3-draft"

CITATION_RULES = """## Citations in this specialist

The acceptance checks read [KB:<file>#p<N>] and [REQ:R-###] citations. A
sentence that states a fact about the customer is grounded only by a
[KB:...] citation whose passage contains its numbers and certifications. A
[REQ:...] citation grounds no company fact and no certification: put it
right after a requirement figure you restate in requirement words ("above
the required 99.5% [REQ:R-010]"), beside the KB-cited fact that meets it.
The claim ledger (record_source on the knowledge-base file, record_claim
with a verbatim quote) backs the key past-performance and staffing claims;
you may put a claim's [C#] id next to the [KB:...] citation, never instead
of it."""


def engagement_mode(intake: Mapping[str, Any] | None) -> str:
    """The intake's engagement mode, normalized ("" when not given)."""
    value = (intake or {}).get("engagement_mode")
    return value.strip().lower() if isinstance(value, str) else ""


class ProposalWriter(Specialist):

    def extra_checks(self) -> list[dict[str, Any]]:
        defs = super().extra_checks()
        items = defs.items() if isinstance(defs, Mapping) else ((d["name"], d["function"]) for d in defs)
        return [{"name": name, "function": fn, "kind": "automated"} for name, fn in items]

    def validate_intake(self, intake: Mapping[str, Any]) -> list[MissingInput]:
        missing = super().validate_intake(intake)
        raw = intake.get("engagement_mode")
        if raw not in (None, "") and engagement_mode(intake) not in ENGAGEMENT_MODES:
            question = next(f.question for f in self.manifest.intake if f.field == "engagement_mode")
            missing.append(MissingInput(
                field="engagement_mode", blocking=True,
                question=f"{question} (got {raw!r}; expected one of {', '.join(ENGAGEMENT_MODES)})"))
        return missing

    def propose_milestones(self, intake: Mapping[str, Any] | None = None) -> list[MilestoneSpec]:
        milestones = super().propose_milestones(intake)
        if engagement_mode(intake) == "questionnaire":
            for m in milestones:
                if m.id == DRAFT_MILESTONE and ANSWERS_PATH not in m.deliverables:
                    m.deliverables.append(ANSWERS_PATH)
        return milestones

    def system_prompt(self, brief: Brief, milestone: MilestoneSpec) -> str:
        return super().system_prompt(brief, milestone) + "\n\n" + CITATION_RULES

    def finalize(self, ctx: ToolContext, milestone: MilestoneSpec, outcome: RunOutcome) -> RunOutcome:
        if (milestone.id == DRAFT_MILESTONE and outcome.status == "submitted"
                and ctx.path(ANSWERS_PATH).is_file() and ANSWERS_PATH not in outcome.artifacts):
            outcome.artifacts.append(ANSWERS_PATH)
        return outcome
