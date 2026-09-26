"""Specialist base (scoping, prompts, run_milestone status rules), registry,
model selection, evals and the CLI - on a manifest-only specialist in tmp_path."""
import copy
import io
import json

import pytest
import yaml

from agentkit.__main__ import main
from agentkit.errors import AgentKitError
from agentkit.evals import load_cases, run_evals
from agentkit.llm import ScriptedAdapter
from agentkit.loop import RunOutcome
from agentkit.manifest import load_manifest
from agentkit.registry import list_specialists, load_specialist
from agentkit.specialist import RunContext, Specialist, build_adapter, resolve_model_refs
from agentkit.types import AcceptanceCriterion, Brief, CheckResult

REPORT = "deliverables/m1/report.md"
MANIFEST = {
    "schema_version": 1, "slug": "demo-writer", "name": "Demo Writer", "version": "0.1.0",
    "summary": "Writes a short report.",
    "listing": {"category": "Research", "pricing": {"typical_low": 100, "typical_high": 400}},
    "profile": "docs",
    "prompts": {"system": "prompts/system.md", "include": ["playbook/method.md"]},
    "tools": ["read_file", "write_file", "list_files", "ask_client", "submit_milestone"],
    "limits": {"max_steps": 10, "max_tokens": 100000, "max_usd": 5, "max_wall_minutes": 10},
    "human_gate": {"required": True, "reviewer_role": "editor", "disclaimer": "Draft for editor review."},
    "intake": [{"field": "topic", "question": "What topic?"},
               {"field": "tone", "question": "Which tone?", "required": False}],
    "estimate": {"usd_per_hour": 5.0},
    "milestones": [
        {"id": "m1", "title": "Report", "deliverables": [REPORT], "hours": [1, 3],
         "acceptance": [{"check": "file_exists", "params": {"path": REPORT}},
                        {"check": "disclaimer_present", "params": {"path": REPORT}},
                        {"check": "human_signoff", "kind": "human"}]},
        {"id": "m2", "title": "Follow-up", "hours": [2, 4]},
    ],
}
GOOD_REPORT = "# Report\n\nFindings.\n\nDraft for editor review.\n"


@pytest.fixture
def root(tmp_path):
    pkg = tmp_path / "specs" / "demo_writer"
    (pkg / "prompts").mkdir(parents=True)
    (pkg / "playbook").mkdir()
    (pkg / "prompts" / "system.md").write_text("You are a careful writer.", encoding="utf-8")
    (pkg / "playbook" / "method.md").write_text("Outline first.", encoding="utf-8")
    (pkg / "agent.yaml").write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    return tmp_path / "specs"


@pytest.fixture
def spec(root):
    return Specialist(load_manifest(root / "demo_writer"))


def _brief(**kw):
    return Brief(engagement_id="e1", specialist="demo-writer", objective="Write about widgets",
                 intake={"topic": "widgets"}, **kw)


def _plan(report=GOOD_REPORT, submit=True):
    plan = [("write_file", {"path": REPORT, "content": report})]
    if submit:
        plan.append(("submit_milestone", {"summary": "Report written", "artifacts": [REPORT]}))
    return ScriptedAdapter.from_tool_plan(plan)


# --- scoping ---------------------------------------------------------------------

def test_validate_intake_estimate_milestones(spec):
    missing = spec.validate_intake({"topic": ""})
    assert [(m.field, m.blocking) for m in missing] == [("topic", True), ("tone", False)]
    assert spec.validate_intake({"topic": "x", "tone": "dry"}) == []
    est = spec.estimate({})
    assert (est.hours_low, est.hours_high, est.cost_usd_low, est.cost_usd_high) == (3.0, 7.0, 15.0, 35.0)
    assert (est.price_usd_low, est.price_usd_high) == (100.0, 400.0)
    ms = spec.propose_milestones({})
    ms[0].title = "mutated"
    assert spec.manifest.milestones[0].title == "Report"


def test_prompts_include_playbook_rules_and_gate(spec):
    brief = _brief(answers={"Which tone?": "dry"}, sow_text="Deliver one report.")
    m = spec.manifest.milestone("m1")
    system = spec.system_prompt(brief, m)
    assert "careful writer" in system and "Outline first." in system
    assert "submit_milestone" in system and "Draft for editor review." in system
    task = spec.task_prompt(brief, m)
    assert "Milestone m1: Report" in task and REPORT in task and "[human] human_signoff" in task
    assert '<untrusted source="client-intake">' in task and "Deliver one report." in task


def test_intake_changes_egress_only_through_the_manifest_opt_in(spec):
    from agentkit.errors import ManifestError, PolicyViolation
    from agentkit.manifest import EgressConfig, IntakeField

    spec.manifest.egress = EgressConfig(mode="allowlist", allow=["api.osv.dev"])
    wide = _brief()
    wide.intake["egress_allow"] = ["*"]                       # the old, undocumented key: ignored
    with pytest.raises(PolicyViolation):
        spec.policy(wide).check_url("https://exfil.example.net/")

    spec.manifest.intake.append(IntakeField("allowed_domains", "Which domains?", required=False))
    spec.manifest.egress = EgressConfig(mode="allowlist", allow=["*"], intake_field="allowed_domains")
    narrow = _brief()
    narrow.intake["allowed_domains"] = ["census.gov"]
    gate = spec.policy(narrow)
    assert gate.check_url("https://census.gov/x") == "census.gov"
    with pytest.raises(PolicyViolation):
        gate.check_url("https://exfil.example.net/")
    assert spec.policy(_brief()).check_url("https://any.example.org/")   # no value: manifest list

    spec.manifest.egress = EgressConfig(mode="allowlist", allow=["api.osv.dev"],
                                        intake_field="allowed_domains", intake_mode="extend")
    narrow.intake["allowed_domains"] = ["*"]
    with pytest.raises(PolicyViolation):
        spec.policy(narrow)
    narrow.intake["allowed_domains"] = "census.gov"
    with pytest.raises(AgentKitError, match="list of hostnames"):
        spec.policy(narrow)

    data = copy.deepcopy(MANIFEST)
    data["egress"] = {"mode": "allowlist", "allow": ["*"], "intake_field": "nope"}
    from agentkit.manifest import parse_manifest
    with pytest.raises(ManifestError, match="egress.intake_field"):
        parse_manifest(data)


def test_kit_rules_follow_the_enabled_tools_and_egress(spec):
    from agentkit.specialist import KIT_RULES
    brief = _brief()
    system = spec.system_prompt(brief, spec.manifest.milestone("m1"))
    # demo-writer has no ledger tools and no network
    assert "record_claim" not in system and "http_fetch" not in system
    assert "no network access" in system and "ask_client" in system
    assert "record_claim" in KIT_RULES and "http_fetch" in KIT_RULES


def test_disclaimer_goes_where_it_is_checked(spec):
    brief = _brief()
    m1 = spec.system_prompt(brief, spec.manifest.milestone("m1"))
    assert f"Include this disclaimer verbatim in {REPORT}:\nDraft for editor review." in m1
    assert "disclaimer in every deliverable" not in m1
    m2 = spec.system_prompt(brief, spec.manifest.milestone("m2"))       # no disclaimer_present check
    assert "not data files such as CSV or JSON" in m2 and "Draft for editor review." in m2


def test_tool_registry_follows_manifest(spec):
    assert spec.tool_registry().names() == MANIFEST["tools"]
    spec.manifest.tools.append("teleport")
    with pytest.raises(AgentKitError, match="teleport"):
        spec.tool_registry()


# --- run_milestone -----------------------------------------------------------------

def test_run_milestone_ready_for_review(spec, tmp_path):
    ws = tmp_path / "ws"
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=_plan()), "m1")
    assert sub.status == "ready_for_review"
    assert [a.path for a in sub.artifacts] == [REPORT] and len(sub.artifacts[0].sha256) == 64
    assert [(r.check, r.passed) for r in sub.check_results] == [
        ("file_exists", True), ("disclaimer_present", True), ("human_signoff", None)]
    assert sub.human_review.required and sub.human_review.reviewer_role == "editor"
    assert sub.evidence_hash.startswith("0x") and len(sub.evidence_hash) == 66
    saved = json.loads((ws / ".agentkit/submissions/m1.json").read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == sub.evidence_hash
    assert (ws / ".agentkit/journal.jsonl").read_text(encoding="utf-8").count('"submission"') == 1


def test_submission_is_redacted_before_hashing(spec, tmp_path):
    from agentkit.evidence import evidence_hash
    from agentkit.types import Submission

    secret = "sk-" + "r" * 20
    adapter = ScriptedAdapter.from_tool_plan([
        ("write_file", {"path": REPORT, "content": GOOD_REPORT}),
        ("submit_milestone", {"summary": f"Done; the key was {secret}", "artifacts": [REPORT]})])
    ws = tmp_path / "ws"
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=adapter,
                                        env={"OPENAI_API_KEY": secret}), "m1")
    assert secret not in sub.summary and "[REDACTED]" in sub.summary
    saved = (ws / ".agentkit/submissions/m1.json").read_text(encoding="utf-8")
    assert secret not in saved and secret not in (ws / ".agentkit/journal.jsonl").read_text(encoding="utf-8")
    assert evidence_hash(Submission.from_dict(json.loads(saved))) == sub.evidence_hash


def test_submission_binds_the_sow_milestone_index(spec, tmp_path):
    import hashlib

    from agentkit.evidence import platform_evidence

    def run(name, brief, **kw):
        return spec.run_milestone(RunContext(brief=brief, workspace=tmp_path / name, adapter=_plan(),
                                             **kw), "m1")

    first = run("a", _brief())                          # the brief lists no milestones: manifest order
    assert first.milestone_idx == 0
    assert first.evidence_hash == "0x" + hashlib.sha256(platform_evidence(first).encode()).hexdigest()
    m1, m2 = spec.manifest.milestone("m1"), spec.manifest.milestone("m2")
    assert run("b", _brief(milestones=[m2, m1])).milestone_idx == 1   # the brief's (SOW) order
    given = run("c", _brief(milestones=[m2, m1]), milestone_idx=4)     # the harness knows best
    assert given.milestone_idx == 4 and given.evidence_hash != first.evidence_hash
    assert json.loads(platform_evidence(given))["milestone_idx"] == 4
    with pytest.raises(AgentKitError, match="SOW index"):
        spec.milestone_index(_brief(), "m9")
    for bad in (-1, True):
        with pytest.raises(AgentKitError, match="milestone_idx"):
            spec.milestone_index(_brief(), "m1", bad)


def test_submission_cost_is_kept_to_micro_usd(spec, tmp_path):
    from agentkit.evidence import submission_record
    from agentkit.types import Usage

    adapter = ScriptedAdapter.from_tool_plan([
        ("write_file", {"path": REPORT, "content": GOOD_REPORT}),
        ("submit_milestone", {"summary": "Report written", "artifacts": [REPORT]})],
        usage_per_step=Usage(10, 5, cost_usd=0.1234567))
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path / "ws", adapter=adapter), "m1")
    assert sub.usage.cost_usd == 0.246913           # two steps, rounded to micro-USD
    assert submission_record(sub)["usage"]["cost_micro_usd"] == 246913


def test_submission_without_human_review_fails_closed():
    from agentkit.types import Submission
    sub = Submission.from_dict({"engagement_id": "e", "milestone_id": "m", "status": "ready_for_review"})
    assert sub.human_review.required is True


def test_run_milestone_needs_revision_when_check_fails(spec, tmp_path):
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path / "ws",
                                        adapter=_plan(report="# Report\nNo disclaimer.\n")), "m1")
    assert sub.status == "needs_revision"
    assert sub.human_review.required


def test_run_milestone_incomplete_without_submit(spec, tmp_path):
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path / "ws",
                                        adapter=_plan(submit=False)), "m1")
    assert sub.status == "incomplete"
    assert [a.path for a in sub.artifacts] == [REPORT]      # declared deliverable still hashed


def test_run_milestone_budget_exceeded(spec, tmp_path):
    spec.manifest.limits.max_steps = 1
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path / "ws", adapter=_plan()), "m1")
    assert sub.status == "budget_exceeded"


def test_run_milestone_errors(spec, tmp_path):
    with pytest.raises(AgentKitError, match="adapter"):
        spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path), "m1")
    with pytest.raises(AgentKitError, match="unknown milestone"):
        spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path, adapter=_plan()), "m9")


def test_brief_milestone_cannot_drop_or_rekind_manifest_checks(spec, tmp_path):
    from agentkit.types import AcceptanceCriterion, MilestoneSpec
    loose = MilestoneSpec(id="m1", title="Report", deliverables=[REPORT], acceptance=[
        AcceptanceCriterion("word_count", params={"path": REPORT, "min": 1})])
    sub = spec.run_milestone(RunContext(brief=_brief(milestones=[loose]), workspace=tmp_path / "ws",
                                        adapter=_plan(report="# Report\nNo disclaimer.\n")), "m1")
    assert [r.check for r in sub.check_results] == ["file_exists", "disclaimer_present", "human_signoff",
                                                    "word_count"]
    assert sub.status == "needs_revision"      # the manifest's disclaimer check still applies


@pytest.mark.parametrize("milestone,match", [
    ({"id": "../../../escaped-submission", "title": "x",
      "acceptance": [{"check": "file_exists", "params": {"path": REPORT}}]}, "id"),
    ({"id": "m7", "title": "x", "deliverables": ["C:/outside/x.md"],
      "acceptance": [{"check": "file_exists", "params": {"path": REPORT}}]}, "deliverables"),
    ({"id": "m7", "title": "x", "deliverables": ["//host/share/x.md"],
      "acceptance": [{"check": "file_exists", "params": {"path": REPORT}}]}, "deliverables"),
    ({"id": "m1", "title": "x", "acceptance": [{"check": "file_exists", "kind": "optional"}]}, "kind"),
    ({"id": "m7", "title": "x", "acceptance": [{"check": "human_signoff", "kind": "human"}]},
     "no automated acceptance check"),
])
def test_bad_brief_milestones_are_refused_before_anything_runs(spec, tmp_path, milestone, match):
    from agentkit.types import MilestoneSpec
    ws = tmp_path / "a" / "b" / "ws"
    brief = _brief(milestones=[MilestoneSpec.from_dict(milestone)])
    adapter = _plan()
    with pytest.raises(AgentKitError, match=match):
        spec.run_milestone(RunContext(brief=brief, workspace=ws, adapter=adapter), milestone["id"])
    assert adapter.calls == [] and not list(tmp_path.rglob("*.json"))


def test_brief_only_milestone_with_an_automated_check_runs(spec, tmp_path):
    from agentkit.types import MilestoneSpec
    extra = MilestoneSpec.from_dict({"id": "m7-extra", "title": "Extra", "deliverables": [REPORT],
                                     "acceptance": [{"check": "file_exists", "params": {"path": REPORT}}]})
    sub = spec.run_milestone(RunContext(brief=_brief(milestones=[extra]), workspace=tmp_path / "ws",
                                        adapter=_plan()), "m7-extra")
    assert sub.status == "ready_for_review"
    assert (tmp_path / "ws/.agentkit/submissions/m7-extra.json").exists()


def test_resuming_a_submitted_milestone_returns_the_saved_submission(spec, tmp_path):
    from agentkit.types import ModelResponse, ToolCall, Usage
    rubric_dir = spec.manifest.base_dir / "rubrics"
    rubric_dir.mkdir()
    (rubric_dir / "r.yaml").write_text("criteria:\n  - {id: clear, description: Clear}\n", encoding="utf-8")
    spec.manifest.milestones[0].acceptance.append(AcceptanceCriterion(
        "rubric_grader", kind="rubric", params={"rubric": "rubrics/r.yaml", "paths": [REPORT]}))

    def grader(score):
        call = ToolCall("j", "score_rubric", {"scores": [{"id": "clear", "score": score, "rationale": ""}]})
        return ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                              usage=Usage(), model="grader")])

    ws = tmp_path / "ws"
    first = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=_plan(), grader=grader(0.9)), "m1")
    assert first.status == "ready_for_review"
    later_grader, adapter = grader(0.1), _plan()
    again = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=adapter, grader=later_grader,
                                          resume=True), "m1")
    assert again.evidence_hash == first.evidence_hash and again.status == "ready_for_review"
    assert adapter.calls == [] and later_grader.calls == []
    saved = json.loads((ws / ".agentkit/submissions/m1.json").read_text(encoding="utf-8"))
    assert saved["evidence_hash"] == first.evidence_hash


def test_resume_binds_a_saved_submission_to_a_newly_given_milestone_idx(spec, tmp_path):
    from agentkit.evidence import evidence_hash, platform_evidence
    from agentkit.types import Submission

    ws = tmp_path / "ws"
    first = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=_plan()), "m1")
    assert first.milestone_idx == 0                        # defaulted to the manifest position
    adapter = _plan()
    again = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=adapter, resume=True,
                                          milestone_idx=2), "m1")
    assert adapter.calls == [] and again.milestone_idx == 2
    assert again.evidence_hash == evidence_hash(again) != first.evidence_hash
    assert '"milestone_idx":2' in platform_evidence(again, 2)
    saved = Submission.from_dict(json.loads((ws / ".agentkit/submissions/m1.json").read_text(
        encoding="utf-8")))
    assert saved.milestone_idx == 2 and saved.evidence_hash == again.evidence_hash
    # resuming without an index keeps the saved one
    kept = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=_plan(), resume=True), "m1")
    assert kept.milestone_idx == 2 and kept.evidence_hash == again.evidence_hash


@pytest.mark.parametrize("outcome,results,status", [
    ("submitted", [CheckResult("a", True), CheckResult("r", None, "rubric"), CheckResult("h", None, "human")],
     "ready_for_review"),
    ("submitted", [CheckResult("a", None)], "needs_revision"),
    ("submitted", [CheckResult("r", False, "rubric")], "needs_revision"),
    ("stuck", [CheckResult("a", True)], "incomplete"),
    ("refused", [], "incomplete"),
    ("budget_exceeded", [CheckResult("a", True)], "budget_exceeded"),
    ("model_error", [CheckResult("a", True)], "incomplete"),
    ("submitted", [CheckResult("a", True), CheckResult("x", False, "Automated")], "needs_revision"),
    ("submitted", [CheckResult("x", None, "optional")], "needs_revision"),
])
def test_status_rules(outcome, results, status):
    assert Specialist.status_for(RunOutcome(status=outcome), results) == status


def test_check_reruns_on_workspace(spec, tmp_path):
    ws = tmp_path / "ws"
    (ws / "deliverables/m1").mkdir(parents=True)
    (ws / REPORT).write_text(GOOD_REPORT, encoding="utf-8")
    results = spec.check(ws, "m1")
    assert [r.passed for r in results] == [True, True, None]


# --- model selection --------------------------------------------------------------

def test_resolve_model_refs_precedence(spec):
    m = spec.manifest
    assert resolve_model_refs(m, env={}) == ("anthropic:claude-opus-5", [], "anthropic:claude-sonnet-5")
    env = {"AGENTKIT_MODEL": "openai:gpt-x", "AGENTKIT_FALLBACK_MODELS": "gemini:g1, openai:gpt-x,",
           "AGENTKIT_GRADER_MODEL": "gemini:grader", "AGENTKIT_ALLOW_MODEL_OVERRIDE": "1"}
    assert resolve_model_refs(m, env=env) == ("openai:gpt-x", ["gemini:g1"], "gemini:grader")
    assert resolve_model_refs(m, model="ollama:q:7b", env=env)[0] == "ollama:q:7b"


@pytest.mark.parametrize("allow", [None, "0", "true", "yes", ""])
def test_models_are_pinned_to_the_manifest_unless_overrides_are_allowed(spec, allow):
    from agentkit.specialist import ignored_model_overrides

    m = spec.manifest
    env = {"AGENTKIT_MODEL": "openai:gpt-x", "AGENTKIT_FALLBACK_MODELS": "gemini:g1",
           "AGENTKIT_GRADER_MODEL": "gemini:grader"}
    if allow is not None:
        env["AGENTKIT_ALLOW_MODEL_OVERRIDE"] = allow
    pinned = ("anthropic:claude-opus-5", [], "anthropic:claude-sonnet-5")
    assert resolve_model_refs(m, model="ollama:q:7b", env=env) == pinned
    assert ignored_model_overrides(model="ollama:q:7b", env=env) == [
        "--model", "AGENTKIT_MODEL", "AGENTKIT_FALLBACK_MODELS", "AGENTKIT_GRADER_MODEL"]
    assert ignored_model_overrides(env={}) == []
    assert ignored_model_overrides(model="x:y", env={"AGENTKIT_ALLOW_MODEL_OVERRIDE": "1"}) == []


def test_build_adapter_uses_the_fallback_chain_with_options():
    from agentkit.llm import FallbackAdapter
    from agentkit.llm.anthropic import AnthropicAdapter

    env = {"OPENAI_API_KEY": "sk-" + "t" * 12}
    chain = build_adapter("anthropic:claude-opus-5", ["openai:gpt-x", "anthropic:claude-opus-5"], env=env,
                          options={"anthropic": {"effort": "xhigh"}}, on_switch=lambda *a: None)
    assert isinstance(chain, FallbackAdapter) and len(chain.adapters) == 2       # duplicate dropped
    assert chain.adapters[0].options == {"effort": "xhigh"} and chain.on_switch is not None
    assert isinstance(build_adapter("anthropic:claude-sonnet-5", [], env={}), AnthropicAdapter)


def test_fallback_switches_are_journaled(spec, tmp_path):
    from agentkit.llm import FallbackAdapter
    from agentkit.types import ModelResponse, Usage

    refuse = ScriptedAdapter([ModelResponse(text="", tool_calls=[], stop_reason="refusal",
                                            usage=Usage(1, 1), model="a")], model="a")
    chain = FallbackAdapter([refuse, _plan()])
    ws = tmp_path / "ws"
    sub = spec.run_milestone(RunContext(brief=_brief(), workspace=ws, adapter=chain), "m1")
    assert sub.status == "ready_for_review" and chain.on_switch is None     # hook removed after the run
    journal = (ws / ".agentkit/journal.jsonl").read_text(encoding="utf-8")
    assert '"model_fallback"' in journal and '"scripted:a"' in journal


def test_manifest_model_options_are_validated_and_reach_the_default_factory(root, monkeypatch):
    import agentkit.__main__ as cli
    from agentkit.errors import ManifestError
    from agentkit.manifest import parse_manifest

    data = copy.deepcopy(MANIFEST)
    data["models"] = {"options": {"anthropic": {"effort": "xhigh", "server_fallbacks": False}}}
    assert parse_manifest(data).models.options["anthropic"]["effort"] == "xhigh"
    for bad in ({"nope": {}}, {"anthropic": "xhigh"}):
        data["models"] = {"options": bad}
        with pytest.raises(ManifestError, match="models.options"):
            parse_manifest(data)
    manifest = copy.deepcopy(MANIFEST)
    manifest["models"] = {"options": {"anthropic": {"effort": "low"}}}
    (root / "demo_writer" / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    seen = []

    def fake_build(primary, fallbacks, env=None, options=None):
        seen.append(options)
        return _plan()

    monkeypatch.setattr(cli, "build_adapter", fake_build)
    cli.main(["--root", str(root), "--package", "", "check", "demo-writer", "--milestone", "m1",
              "--workspace", str(root / "ws"), "--grader"], stdout=io.StringIO(), env={})
    assert seen == [{"anthropic": {"effort": "low"}}]


# --- registry ----------------------------------------------------------------------

def test_registry_lists_and_loads_manifest_only_specialist(root):
    (root / "broken").mkdir()
    (root / "broken" / "agent.yaml").write_text("slug: [", encoding="utf-8")
    assert [m.slug for m in list_specialists(root)] == ["demo-writer"]
    spec = load_specialist("demo-writer", root=root, package=None)
    assert type(spec) is Specialist and spec.package == "demo_writer"
    assert spec.extra_tools() == [] and spec.extra_checks() == {}
    with pytest.raises(AgentKitError, match="unknown specialist"):
        load_specialist("nope", root=root, package=None)


def test_a_broken_import_inside_tools_py_is_not_mistaken_for_no_tools(root, monkeypatch):
    # "specialists.demo_writer.tools" starts with "spec"; a missing module named
    # "spec" inside tools.py must surface, not silently drop the domain tools.
    import sys
    pkgs = root.parent / "pkgs"
    (pkgs / "specbroken_pack").mkdir(parents=True)
    (pkgs / "specbroken_pack" / "__init__.py").write_text("", encoding="utf-8")
    (pkgs / "specbroken_pack" / "tools.py").write_text("import spec\nTOOL_DEFS = []\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(pkgs))
    manifest = load_manifest(root / "demo_writer")
    try:
        with pytest.raises(ModuleNotFoundError):
            Specialist(manifest, package="specbroken_pack").extra_tools()
        assert Specialist(manifest, package="no_such_pack").extra_tools() == []
    finally:
        for name in [n for n in sys.modules if n.startswith("specbroken_pack")]:
            del sys.modules[name]


def _package(parent, name, *, agent_py=None, init_py=""):
    """A specialist package parent/name whose modules write `imported-*` next
    to parent when they run."""
    pkg = parent / name
    (pkg / "prompts").mkdir(parents=True)
    (pkg / "playbook").mkdir()
    (pkg / "prompts" / "system.md").write_text("You are a careful writer.", encoding="utf-8")
    (pkg / "playbook" / "method.md").write_text("Outline first.", encoding="utf-8")
    manifest = copy.deepcopy(MANIFEST)
    manifest["slug"] = name.replace("_", "-")
    (pkg / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (pkg / "__init__.py").write_text(init_py, encoding="utf-8")
    (pkg / "agent.py").write_text(agent_py or (
        "from pathlib import Path\nfrom agentkit.specialist import Specialist\n"
        "(Path(__file__).parent.parent / 'imported-agent').write_text('x')\n"
        "class Pack(Specialist):\n    pass\n"), encoding="utf-8")
    return pkg


def _forget(prefix):
    import importlib
    import sys
    for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def test_run_checks_the_spec_hash_before_importing_the_package(tmp_path, monkeypatch):
    from agentkit.manifest import spec_hash
    root = tmp_path / "specs"
    pkg = _package(root, "stamp_pack")
    monkeypatch.syspath_prepend(str(root))
    _forget("stamp_pack")
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps(_brief().to_dict()), encoding="utf-8")
    stamped = spec_hash(pkg)
    (pkg / "agent.py").write_text((pkg / "agent.py").read_text(encoding="utf-8") + "# drift\n",
                                  encoding="utf-8")
    try:
        code, _ = _cli(root, "run", "stamp-pack", "--brief", str(brief), "--milestone", "m1",
                       "--workspace", str(tmp_path / "ws"), "--spec-hash", stamped, "--no-grader",
                       factory=lambda *a: _plan())
        assert code == 2
        assert not (root / "imported-agent").exists()          # the drifted agent.py never ran
        code, _ = _cli(root, "run", "stamp-pack", "--brief", str(brief), "--milestone", "m1",
                       "--workspace", str(tmp_path / "ws"), "--spec-hash", spec_hash(pkg),
                       "--no-grader", factory=lambda *a: _plan())
        assert code == 0 and (root / "imported-agent").exists()
    finally:
        _forget("stamp_pack")


def test_a_same_named_package_earlier_on_sys_path_is_refused_before_it_runs(tmp_path, monkeypatch):
    legit = tmp_path / "legit"
    _package(legit, "shadow_pack")
    rogue = tmp_path / "rogue"
    marker = "(__import__('pathlib').Path(__file__).parent.parent / 'imported-rogue').write_text('x')\n"
    _package(rogue, "shadow_pack", agent_py=marker, init_py=marker)
    monkeypatch.syspath_prepend(str(legit))
    monkeypatch.syspath_prepend(str(rogue))       # first, like a workspace used as the cwd
    _forget("shadow_pack")
    try:
        with pytest.raises(AgentKitError, match="refusing to import shadow_pack"):
            load_specialist("shadow-pack", root=legit, package=None)
        assert not (rogue / "imported-rogue").exists()
        # the same guard for a dotted package: specs.shadow_pack under legit2
        legit2, rogue2 = tmp_path / "legit2" / "shadow_specs", tmp_path / "rogue2" / "shadow_specs"
        _package(legit2, "shadow_pack")
        (legit2 / "__init__.py").write_text("", encoding="utf-8")
        rogue2.mkdir(parents=True)
        (rogue2 / "__init__.py").write_text(marker, encoding="utf-8")
        monkeypatch.syspath_prepend(str(legit2.parent))
        monkeypatch.syspath_prepend(str(rogue2.parent))
        _forget("shadow_specs")
        with pytest.raises(AgentKitError, match="refusing to import shadow_specs.shadow_pack"):
            load_specialist("shadow-pack", root=legit2, package="shadow_specs")
        assert not (rogue2.parent / "imported-rogue").exists()
        # with the hashed package first on sys.path it loads
        monkeypatch.syspath_prepend(str(legit))
        _forget("shadow_pack")
        assert type(load_specialist("shadow-pack", root=legit, package=None)).__name__ == "Pack"
    finally:
        _forget("shadow_pack")
        _forget("shadow_specs")


def test_domain_modules_of_a_manifest_only_package_come_from_its_directory(root, tmp_path,
                                                                            monkeypatch):
    spec = load_specialist("demo-writer", root=root, package=None)    # not importable: no code
    rogue = tmp_path / "rogue"
    (rogue / "demo_writer").mkdir(parents=True)
    (rogue / "demo_writer" / "__init__.py").write_text(
        "(__import__('pathlib').Path(__file__).parent / 'ran').write_text('x')\n", encoding="utf-8")
    (rogue / "demo_writer" / "tools.py").write_text("TOOL_DEFS = []\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(rogue))       # appears mid-run, e.g. written by the model
    _forget("demo_writer")
    try:
        with pytest.raises(AgentKitError, match="refusing to import demo_writer"):
            spec.extra_tools()
        assert not (rogue / "demo_writer" / "ran").exists()
    finally:
        _forget("demo_writer")


def test_module_file_check_accepts_nested_modules_and_refuses_outside_ones(tmp_path):
    from types import SimpleNamespace

    from agentkit.specialist import check_module_file

    pkg = tmp_path / "pack"
    (pkg / "agent").mkdir(parents=True)
    check_module_file(SimpleNamespace(__name__="pack.agent", __file__=str(pkg / "agent.py")), pkg)
    check_module_file(SimpleNamespace(__name__="pack.agent",
                                      __file__=str(pkg / "agent" / "__init__.py")), pkg)
    for origin in (str(tmp_path / "elsewhere" / "agent.py"), str(tmp_path / "pack2" / "agent.py"), None):
        with pytest.raises(AgentKitError, match="was loaded from"):
            check_module_file(SimpleNamespace(__name__="pack.agent", __file__=origin), pkg)


def test_cli_refuses_a_working_directory_inside_the_workspace(root, tmp_path, monkeypatch, capsys):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps(_brief().to_dict()), encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / "deliverables").mkdir(parents=True)
    calls = []
    monkeypatch.chdir(ws / "deliverables")
    code, _ = _cli(root, "run", "demo-writer", "--brief", str(brief), "--milestone", "m1",
                   "--workspace", str(ws), factory=lambda *a: calls.append(a) or _plan())
    assert code == 2 and calls == []
    assert "inside the workspace" in capsys.readouterr().err
    assert _cli(root, "check", "demo-writer", "--milestone", "m1", "--workspace", str(ws))[0] == 2
    monkeypatch.chdir(tmp_path)
    assert _cli(root, "check", "demo-writer", "--milestone", "m1", "--workspace", str(ws))[0] == 1


def test_repo_specialists_listing_does_not_crash():
    assert isinstance(list_specialists(), list)


# --- evals ---------------------------------------------------------------------------

def test_evals_run_cases_with_fixtures(root, tmp_path):
    evals = root / "demo_writer" / "evals"
    (evals / "cases").mkdir(parents=True)
    (evals / "fixtures" / "set1").mkdir(parents=True)
    (evals / "fixtures" / "set1" / "notes.txt").write_text("widgets", encoding="utf-8")
    (evals / "fixtures" / "brief.md").write_text("brief", encoding="utf-8")
    (evals / "cases" / "c1.json").write_text(json.dumps({
        "name": "c1", "milestone": "m1", "brief": {"intake": {"topic": "widgets"}}, "notes": "n",
        "fixture": "set1", "fixtures": {"inputs/brief.md": "brief.md"}}), encoding="utf-8")
    spec = Specialist(load_manifest(root / "demo_writer"))
    assert [c.name for c in load_cases(spec)] == ["c1"]
    results = run_evals(spec, adapter=_plan(), out_dir=tmp_path / "out")
    assert results[0]["status"] == "ready_for_review"
    assert results[0]["models"] == ["scripted:scripted"]
    assert (tmp_path / "out/c1/inputs/notes.txt").exists() and (tmp_path / "out/c1/inputs/brief.md").exists()


# --- CLI ---------------------------------------------------------------------------------

def _cli(root, *args, factory=None, env=None):
    out = io.StringIO()
    code = main(["--root", str(root), "--package", "", *args], stdout=out, env=env or {},
                adapter_factory=factory)
    return code, out.getvalue()


def test_cli_list_show_estimate_milestones_intake(root, tmp_path):
    code, text = _cli(root, "list")
    assert code == 0 and "demo-writer" in text and "human-gated" in text
    code, text = _cli(root, "show", "demo-writer")
    assert code == 0 and json.loads(text)["tools"] == MANIFEST["tools"]
    code, text = _cli(root, "estimate", "demo-writer")
    assert json.loads(text)["hours_high"] == 7.0
    code, text = _cli(root, "milestones", "demo-writer")
    assert [m["id"] for m in json.loads(text)] == ["m1", "m2"]
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps({"tone": "dry"}), encoding="utf-8")
    code, text = _cli(root, "validate-intake", "demo-writer", "--intake", str(intake))
    assert code == 1 and json.loads(text)[0]["field"] == "topic"
    assert _cli(root, "show", "missing-one")[0] == 2


def test_validate_catches_wiring_problems_before_the_model_runs(spec, tmp_path):
    assert spec.validate() == []
    m1 = spec.manifest.milestones[0]
    m1.acceptance.append(AcceptanceCriterion("disclaimer_presnet", params={"path": REPORT}))
    m1.acceptance.append(AcceptanceCriterion("rubric_grader", kind="rubric", params={"rubric": "rubrics/nope.yaml"}))
    spec.manifest.tools.append("teleport")
    problems = spec.validate()
    assert any("teleport" in p for p in problems)
    assert any("disclaimer_presnet" in p and "unknown check" in p for p in problems)
    assert any("rubrics/nope.yaml" in p for p in problems)
    adapter = _plan()
    with pytest.raises(AgentKitError, match="not ready to run"):
        spec.run_milestone(RunContext(brief=_brief(), workspace=tmp_path / "ws", adapter=adapter), "m1")
    assert adapter.calls == []


def test_cli_validate_list_warnings_and_exit_codes(root, tmp_path, capsys):
    (root / "broken").mkdir()
    (root / "broken" / "agent.yaml").write_text("slug: [", encoding="utf-8")
    code, text = _cli(root, "list")
    assert code == 0 and "demo-writer" in text
    assert "warning: skipped" in capsys.readouterr().err
    code, text = _cli(root, "validate", "demo-writer")
    assert code == 0 and json.loads(text) == []

    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps({"specialist": "demo-writer", "objective": "x"}), encoding="utf-8")
    code, _ = _cli(root, "run", "demo-writer", "--brief", str(brief), "--milestone", "m1",
                   "--workspace", str(tmp_path / "ws"), factory=lambda p, f, e: _plan())
    err = capsys.readouterr().err
    assert code == 2 and "invalid brief" in err and "engagement_id" in err

    brief.write_text(json.dumps(_brief().to_dict()), encoding="utf-8")
    code, _ = _cli(root, "run", "demo-writer", "--brief", str(brief), "--milestone", "m1", "--no-grader",
                   "--workspace", str(tmp_path / "ws2"),
                   factory=lambda p, f, e: _plan(report="# Report\nNo disclaimer.\n"))
    assert code == 3                                     # submitted, but needs_revision


def test_cli_check_fails_on_a_failed_rubric(root, tmp_path):
    from agentkit.types import ModelResponse, ToolCall, Usage
    pack = root / "demo_writer"
    (pack / "rubrics").mkdir()
    (pack / "rubrics" / "r.yaml").write_text("criteria:\n  - {id: clear, description: Clear}\n", encoding="utf-8")
    manifest = copy.deepcopy(MANIFEST)
    manifest["milestones"][0]["acceptance"].append(
        {"check": "rubric_grader", "params": {"rubric": "rubrics/r.yaml", "paths": [REPORT]}})
    (pack / "agent.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / "deliverables/m1").mkdir(parents=True)
    (ws / REPORT).write_text(GOOD_REPORT, encoding="utf-8")

    def low_grader(primary, fallbacks, env):
        call = ToolCall("j", "score_rubric", {"scores": [{"id": "clear", "score": 0.1, "rationale": ""}]})
        return ScriptedAdapter([ModelResponse(text="", tool_calls=[call], stop_reason="tool_use",
                                              usage=Usage(), model="grader")])

    code, text = _cli(root, "check", "demo-writer", "--milestone", "m1", "--workspace", str(ws),
                      "--grader", factory=low_grader)
    results = {r["check"]: r["passed"] for r in json.loads(text)}
    assert results["file_exists"] is True and results["rubric_grader"] is False
    assert code == 1


def test_eval_case_schema_is_validated_with_lenient_fixture_forms(root, tmp_path):
    from agentkit.evals import parse_case
    p = tmp_path / "c.json"
    assert parse_case({"milestone": "m1", "fixtures": "larkspur"}, p).fixture == "larkspur"
    assert parse_case({"milestone": "m1", "fixture": "evals/fixtures/tamarind-loop"}, p).fixture == "tamarind-loop"
    for bad in ([], {"brief": {}}, {"milestone": "m1", "brief": "x"}, {"milestone": "m1", "fixtures": [1]},
                {"milestone": "m1", "fixture": "a", "fixtures": "b"}, {"milestone": "m1", "fixure": "a"}):
        with pytest.raises(AgentKitError, match="eval case c.json"):
            parse_case(bad, p)


def test_cli_run_and_check(root, tmp_path):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps(_brief().to_dict()), encoding="utf-8")
    ws = tmp_path / "ws"
    seen = []

    def factory(primary, fallbacks, env):
        seen.append(primary)
        if primary.startswith("anthropic:claude-sonnet"):
            raise ImportError("no SDK")          # grader unavailable -> rubric pending
        return _plan()

    secret = "sk-" + "q" * 16
    code, text = _cli(root, "run", "demo-writer", "--brief", str(brief), "--milestone", "m1",
                      "--workspace", str(ws), "--model", "openai:gpt-test", "--milestone-idx", "3",
                      factory=factory,
                      env={"OPENAI_API_KEY": secret, "AGENTKIT_ALLOW_MODEL_OVERRIDE": "1"})
    assert code == 0 and seen[0] == "openai:gpt-test"
    saved = json.loads((ws / ".agentkit/submissions/m1.json").read_text(encoding="utf-8"))
    assert saved["milestone_idx"] == 3
    events = [json.loads(line) for line in text.splitlines()]
    assert events[-1]["type"] == "submission" and events[-1]["data"]["status"] == "ready_for_review"
    assert any(e["type"] == "warning" for e in events)
    assert (ws / ".agentkit/submissions/m1.json").exists()
    code, text = _cli(root, "check", "demo-writer", "--milestone", "m1", "--workspace", str(ws))
    assert code == 0 and [r["passed"] for r in json.loads(text)] == [True, True, None]
    (ws / REPORT).write_text("tampered", encoding="utf-8")
    assert _cli(root, "check", "demo-writer", "--milestone", "m1", "--workspace", str(ws))[0] == 1


def test_cli_spec_hash_and_run_refuses_an_unstamped_package(root, tmp_path):
    from agentkit.manifest import spec_hash

    code, text = _cli(root, "spec-hash", "demo-writer")
    stamped = spec_hash(root / "demo_writer")
    assert code == 0 and text == stamped + "\n"
    code, text = _cli(root, "spec-hash", "demo-writer", "--files")
    listing = json.loads(text)
    assert listing["spec_hash"] == stamped
    assert [f["path"] for f in listing["description"]["files"]] == [
        "agent.yaml", "playbook/method.md", "prompts/system.md"]

    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps(_brief().to_dict()), encoding="utf-8")
    calls = []

    def factory(primary, fallbacks, env):
        calls.append(primary)
        return _plan()

    run = ["run", "demo-writer", "--brief", str(brief), "--milestone", "m1", "--no-grader"]
    (root / "demo_writer" / "prompts" / "system.md").write_text("Edited after the stamp.",
                                                                  encoding="utf-8")
    for bad in (stamped, "0x1234", "nope"):
        code, _ = _cli(root, *run, "--workspace", str(tmp_path / "ws-bad"), "--spec-hash", bad,
                       factory=factory)
        assert code == 2
    assert calls == [] and not (tmp_path / "ws-bad").exists()     # nothing was run
    current = spec_hash(root / "demo_writer")
    code, _ = _cli(root, *run, "--workspace", str(tmp_path / "ws"),          # hex case ignored
                   "--spec-hash", "0x" + current[2:].upper(), factory=factory)
    assert code == 0 and calls


def test_cli_run_ignores_model_overrides_in_production(root, tmp_path, capsys):
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps(_brief().to_dict()), encoding="utf-8")
    seen = []

    def factory(primary, fallbacks, env):
        seen.append((primary, fallbacks))
        return _plan()

    code, _ = _cli(root, "run", "demo-writer", "--brief", str(brief), "--milestone", "m1",
                   "--workspace", str(tmp_path / "ws"), "--model", "openai:gpt-test", "--no-grader",
                   factory=factory, env={"AGENTKIT_FALLBACK_MODELS": "gemini:g1"})
    assert code == 0 and seen == [("anthropic:claude-opus-5", [])]
    err = capsys.readouterr().err
    assert "ignoring --model, AGENTKIT_FALLBACK_MODELS" in err
    assert "AGENTKIT_ALLOW_MODEL_OVERRIDE=1" in err
