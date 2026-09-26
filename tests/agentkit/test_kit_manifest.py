"""agent.yaml parsing: strict keys and types, dotted error paths, public listing."""
import copy
import json
import re

import pytest
import yaml

from agentkit.errors import ManifestError
from agentkit.manifest import load_manifest, parse_manifest

BASE = {
    "schema_version": 1,
    "slug": "demo-writer",
    "name": "Demo Writer",
    "version": "0.1.0",
    "summary": "Writes a short report.",
    "listing": {"category": "Research", "description": "Demo", "tags": ["demo"],
                "pricing": {"model": "per_milestone", "currency": "USDC",
                            "typical_low": 100, "typical_high": 400}},
    "models": {"primary": "anthropic:claude-opus-5", "grader": "anthropic:claude-sonnet-5"},
    "profile": "research",
    "prompts": {"system": "prompts/system.md"},
    "tools": ["read_file", "write_file", "submit_milestone"],
    "shell": {"allow": ["python"], "timeout_seconds": 60},
    "egress": {"mode": "allowlist", "allow": ["*.example.org"]},
    "limits": {"max_steps": 20, "max_tokens": 100000, "max_usd": 5, "max_wall_minutes": 10},
    "human_gate": {"required": True, "reviewer_role": "editor", "disclaimer": "Draft only.",
                   "checklist": ["Read it"]},
    "intake": [{"field": "topic", "question": "What topic?", "required": True}],
    "estimate": {"usd_per_hour": 5.0},
    "milestones": [{
        "id": "m1-report", "title": "Report", "description": "Write it",
        "deliverables": ["deliverables/m1/report.md"], "hours": [1, 3],
        "acceptance": [
            {"check": "file_exists", "params": {"path": "deliverables/m1/report.md"}},
            {"check": "human_signoff", "kind": "human", "description": "Editor approves"},
        ],
    }],
}


def test_parses_full_manifest():
    m = parse_manifest(copy.deepcopy(BASE))
    assert m.slug == "demo-writer" and m.package == "demo_writer"
    assert m.listing.pricing.typical_high == 400.0
    assert m.milestones[0].hours == (1.0, 3.0)
    assert m.milestones[0].acceptance[1].kind == "human"
    assert m.shell.allow == ["python"] and m.egress.mode == "allowlist"
    assert m.human_review().required is True
    assert m.milestone("m1-report").title == "Report"


def test_defaults_apply_for_optional_sections():
    data = {k: v for k, v in BASE.items()
            if k not in ("models", "shell", "egress", "limits", "human_gate", "intake", "estimate")}
    m = parse_manifest(data)
    assert m.models.primary == "anthropic:claude-opus-5"
    assert m.egress.mode == "none" and m.human_gate.required is False
    assert m.limits.max_steps == 80


@pytest.mark.parametrize("path,value,where", [
    ("bogus", 1, "bogus"),
    ("listing.pricing.extra", 1, "listing.pricing.extra"),
    ("milestones.0.acceptance.0.chek", "x", "milestones[0].acceptance[0].chek"),
    ("limits.max_steps", "ten", "limits.max_steps"),
    ("limits.max_steps", True, "limits.max_steps"),
    ("human_gate.required", "yes", "human_gate.required"),
    ("profile", "legal", "profile"),
    ("milestones.0.acceptance.1.kind", "maybe", "milestones[0].acceptance[1].kind"),
    ("milestones.0.hours", [1], "milestones[0].hours"),
    ("tools", "read_file", "tools"),
    ("slug", KeyError, "slug"),
    ("schema_version", 2, "schema_version"),
    ("slug", "Demo_Writer", "slug"),
    ("models.primary", "claude-opus-5", "models.primary"),
    ("models.grader", "nope:x", "models.grader"),
    ("limits.max_usd", 0, "limits.max_usd"),
    ("milestones.0.hours", [3, 1], "milestones[0].hours"),
    ("milestones.0.deliverables", ["../x.md"], "milestones[0].deliverables[0]"),
    ("human_gate.reviewer_role", "", "human_gate.reviewer_role"),
    ("milestones", [], "milestones"),
    ("tools", ["read file"], "tools[0]"),
    ("limits.max_usd", float("nan"), "limits.max_usd"),
    ("limits.max_wall_minutes", float("inf"), "limits.max_wall_minutes"),
    ("milestones.0.hours", [float("nan"), float("nan")], "milestones[0].hours[0]"),
    ("milestones.0.id", "../../../escaped", "milestones[0].id"),
    ("milestones.0.id", "m1:a", "milestones[0].id"),
    ("milestones.0.deliverables", ["C:/Windows/x.md"], "milestones[0].deliverables[0]"),
    ("milestones.0.deliverables", ["\\\\host\\share\\x.md"], "milestones[0].deliverables[0]"),
    ("milestones.0.deliverables", ["/etc/x.md"], "milestones[0].deliverables[0]"),
    ("milestones.0.deliverables", ["deliverables\\..\\..\\x.md"], "milestones[0].deliverables[0]"),
    ("milestones.0.deliverables", ["inputs/contract.docx"], "milestones[0].deliverables[0]"),
    ("milestones.0.deliverables", [".agentkit/ledger.json"], "milestones[0].deliverables[0]"),
    ("milestones.0.deliverables", ["deliverables/a.md:hidden"], "milestones[0].deliverables[0]"),
    ("milestones.0.acceptance.1.kind", "automated", None),   # human_signoff: kind inferred
    ("milestones.0.acceptance.0.kind", "human", "milestones[0].acceptance[0].kind"),
])
def test_errors_carry_dotted_path(path, value, where):
    data = _with(path, value)
    if where is None:
        parse_manifest(data)
        return
    with pytest.raises(ManifestError) as exc:
        parse_manifest(data)
    assert exc.value.path == where


def test_builtin_check_kinds_are_fixed():
    data = copy.deepcopy(BASE)
    data["milestones"][0]["acceptance"].append({"check": "rubric_grader", "params": {"rubric": "r.yaml"}})
    m = parse_manifest(data)
    assert [a.kind for a in m.milestones[0].acceptance] == ["automated", "human", "rubric"]
    data["milestones"][0]["acceptance"][-1]["kind"] = "human"
    with pytest.raises(ManifestError, match="rubric check"):
        parse_manifest(data)


def test_duplicate_yaml_keys_rejected(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("You write.", encoding="utf-8")
    text = yaml.safe_dump(BASE) + "limits: {max_steps: 999}\n"
    (tmp_path / "agent.yaml").write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["limits"] == {"max_steps": 999}      # what safe_load would do
    with pytest.raises(ManifestError, match="duplicate key 'limits'"):
        load_manifest(tmp_path)
    nested = yaml.safe_dump(BASE).replace("  max_steps: 20\n", "  max_steps: 20\n  max_steps: 5\n")
    (tmp_path / "agent.yaml").write_text(nested, encoding="utf-8")
    with pytest.raises(ManifestError, match="duplicate key 'max_steps'"):
        load_manifest(tmp_path)


def test_parse_milestone_is_strict():
    from agentkit.manifest import parse_milestone
    ok = parse_milestone({"id": "m9", "title": "Extra", "acceptance": [{"check": "human_signoff"}]})
    assert ok.acceptance[0].kind == "human"
    for bad in ({"id": "m9", "title": "x", "surprise": 1},
                {"id": "m9", "title": "x", "acceptance": [{"check": "file_exists", "kind": "optional"}]},
                {"id": "m9", "title": ""}):
        with pytest.raises(ManifestError):
            parse_milestone(bad)


def _with(path, value):
    """BASE with the dotted `path` set to `value` (KeyError deletes it)."""
    data = copy.deepcopy(BASE)
    node = data
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[int(k)] if isinstance(node, list) else node[k]
    last = keys[-1]
    if value is KeyError:
        del node[last]
    else:
        node[last] = value
    return data


def test_duplicate_milestone_ids_rejected():
    data = copy.deepcopy(BASE)
    data["milestones"].append(copy.deepcopy(data["milestones"][0]))
    with pytest.raises(ManifestError) as exc:
        parse_manifest(data)
    assert exc.value.path == "milestones[1].id"


def test_egress_none_with_hosts_rejected():
    data = _with("egress", {"mode": "none", "allow": ["example.org"]})
    with pytest.raises(ManifestError, match="egress.allow"):
        parse_manifest(data)


def test_root_must_be_mapping():
    with pytest.raises(ManifestError):
        parse_manifest(["not", "a", "mapping"])


def test_load_manifest_from_dir_checks_prompt_files(tmp_path):
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path)
    assert exc.value.path == "prompts.system"
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("You write.", encoding="utf-8")
    m = load_manifest(tmp_path)
    assert m.base_dir == tmp_path
    assert m.resolve("prompts/system.md").read_text(encoding="utf-8") == "You write."


def test_load_manifest_bad_yaml_and_missing_file(tmp_path):
    (tmp_path / "agent.yaml").write_text("slug: [unclosed", encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid YAML"):
        load_manifest(tmp_path / "agent.yaml")
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest(tmp_path / "missing.yaml")


def test_public_listing_hides_internals():
    listing = parse_manifest(copy.deepcopy(BASE)).public_listing()
    assert listing["slug"] == "demo-writer"
    assert listing["category"] == "Research"
    assert listing["human_review"] == {"required": True, "reviewer_role": "editor"}
    assert listing["milestones"][0]["hours"] == [1.0, 3.0]
    for private in ("tools", "prompts", "limits", "models", "shell", "egress"):
        assert private not in listing


# --- task price and the operator-stamped manifest bridge ---------------------------

@pytest.mark.parametrize("value,ok", [
    (None, True), (0.25, True), (10, True), (0.000001, True),
    (0, False), (-1, False), (10.01, False), (0.0000001, False), (True, False), ("1", False),
])
def test_task_price_is_small_and_micro_usdc(value, ok):
    data = _with("listing.pricing.task_price_usdc", value)
    if ok:
        m = parse_manifest(data)
        assert m.listing.pricing.task_price_usdc == value
        assert m.public_listing()["pricing"]["task_price_usdc"] == value
        return
    with pytest.raises(ManifestError) as exc:
        parse_manifest(data)
    assert exc.value.path == "listing.pricing.task_price_usdc"


def test_task_price_micro_is_exact():
    from agentkit.manifest import task_price_micro

    assert task_price_micro(parse_manifest(_with("listing.pricing.task_price_usdc", None))) is None
    for value, micro in ((4.1, 4_100_000), (2.01, 2_010_000), (8.03, 8_030_000), (10, 10_000_000),
                         (0.000001, 1), (1.234567, 1_234_567)):
        m = parse_manifest(_with("listing.pricing.task_price_usdc", value))
        assert task_price_micro(m) == micro, value
    for cents in range(1, 1001):          # every cent price the listing allows
        value = cents / 100
        m = parse_manifest(_with("listing.pricing.task_price_usdc", value))
        assert task_price_micro(m) == cents * 10_000, value


def _package(root, name="pkg", manifest=None):
    """A specialist package with every kind of runtime and non-runtime file."""
    pkg = root / name
    for d in ("prompts", "playbook", "rubrics/extra", "evals/cases", "__pycache__",
              "prompts/__pycache__"):
        (pkg / d).mkdir(parents=True, exist_ok=True)
    data = copy.deepcopy(manifest or BASE)
    data["prompts"] = {"system": "prompts/system.md", "include": ["playbook/method.md"]}
    files = {
        "agent.yaml": yaml.safe_dump(data),
        "prompts/system.md": "You write.\nCarefully.\n",
        "playbook/method.md": "Outline first.\n",
        "rubrics/extra/r.yaml": "criteria: []\n",
        "tools.py": "TOOL_DEFS = []\n",
        "__init__.py": "",
        "README.md": "docs",
        "evals/cases/c1.json": "{}",
        "__pycache__/tools.cpython-312.pyc": "x",
        "prompts/__pycache__/junk.pyc": "x",
    }
    for rel, text in files.items():
        (pkg / rel).write_bytes(text.encode("utf-8"))
    return pkg


def test_spec_hash_covers_runtime_files_only(tmp_path):
    from agentkit.manifest import spec_description, spec_hash

    pkg = _package(tmp_path)
    paths = [f["path"] for f in spec_description(pkg)["files"]]
    assert paths == ["__init__.py", "agent.yaml", "playbook/method.md", "prompts/system.md",
                     "rubrics/extra/r.yaml", "tools.py"]
    before = spec_hash(load_manifest(pkg))
    assert before == spec_hash(pkg) == spec_hash(pkg / "agent.yaml")
    assert re.fullmatch(r"0x[0-9a-f]{64}", before)
    # README, evals/ and __pycache__ are not runtime files
    (pkg / "README.md").write_text("changed", encoding="utf-8")
    (pkg / "evals" / "cases" / "c2.json").write_text("{}", encoding="utf-8")
    (pkg / "prompts" / "__pycache__" / "more.pyc").write_bytes(b"y")
    assert spec_hash(pkg) == before
    # any change to a runtime file, or a new one, changes the hash
    for rel, text in (("prompts/system.md", "You write!\n"), ("tools.py", "TOOL_DEFS = [1]\n"),
                      ("rubrics/extra/new.yaml", "x: 1\n"), ("checks.py", "")):
        other = _package(tmp_path, "other")
        (other / rel).write_text(text, encoding="utf-8")
        assert spec_hash(other) != before, rel


def test_spec_hash_ignores_os_and_editor_leftovers(tmp_path):
    from agentkit.manifest import spec_hash

    before = spec_hash(_package(tmp_path, "clean"))
    pkg = _package(tmp_path, "messy")
    for rel in ("playbook/.DS_Store", "prompts/system.md~", "prompts/.system.md.swp",
                "rubrics/Thumbs.db", "playbook/desktop.ini", ".DS_Store", "tools.py~",
                "prompts/#system.md#", "prompts/.ipynb_checkpoints/system.md", ".git/config",
                "lib/__pycache__/helper.cpython-312.pyc", "rubrics/extra/r.yaml.swo"):
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_bytes(b"junk")
    assert spec_hash(pkg) == before


def test_spec_hash_covers_modules_in_subpackages(tmp_path):
    from agentkit.manifest import spec_description, spec_hash

    before = spec_hash(_package(tmp_path, "flat"))
    pkg = _package(tmp_path, "nested")
    (pkg / "lib").mkdir()
    (pkg / "lib" / "helper.py").write_text("RATE = 1\n", encoding="utf-8")
    (pkg / "evals" / "cases" / "gen.py").write_text("x = 1\n", encoding="utf-8")   # not runtime
    assert "lib/helper.py" in [f["path"] for f in spec_description(pkg)["files"]]
    assert "evals/cases/gen.py" not in [f["path"] for f in spec_description(pkg)["files"]]
    assert spec_hash(pkg) != before


def test_spec_hash_ignores_line_endings(tmp_path):
    from agentkit.manifest import spec_hash

    lf, crlf = _package(tmp_path, "lf"), _package(tmp_path, "crlf")
    for path in crlf.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert b"\r\n" in (crlf / "agent.yaml").read_bytes()
    assert spec_hash(crlf) == spec_hash(lf)


def test_spec_hash_is_sha256_of_the_canonical_description(tmp_path):
    """The documented algorithm, recomputed by hand so other languages can match it."""
    import hashlib
    import json

    from agentkit.manifest import spec_hash

    pkg = tmp_path / "tiny"
    (pkg / "prompts").mkdir(parents=True)
    (pkg / "prompts" / "system.md").write_bytes(b"Hi\r\n")
    agent = yaml.safe_dump(BASE).encode("utf-8")
    (pkg / "agent.yaml").write_bytes(agent)
    files = [{"bytes": len(agent), "path": "agent.yaml", "sha256": hashlib.sha256(agent).hexdigest()},
             {"bytes": 3, "path": "prompts/system.md", "sha256": hashlib.sha256(b"Hi\n").hexdigest()}]
    text = json.dumps({"files": files, "v": 1}, sort_keys=True, separators=(",", ":"))
    assert spec_hash(pkg) == "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_spec_hash_refuses_prompts_and_rubrics_it_does_not_cover(tmp_path):
    from agentkit.manifest import spec_description, spec_hash

    data = copy.deepcopy(BASE)
    data["prompts"] = {"system": "prompts/system.md", "include": ["notes/extra.md"]}
    pkg = _package(tmp_path, manifest=data)
    (pkg / "agent.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (pkg / "notes").mkdir()
    (pkg / "notes" / "extra.md").write_text("Unhashed.", encoding="utf-8")
    with pytest.raises(ManifestError) as exc:
        spec_hash(load_manifest(pkg))
    assert exc.value.path == "prompts.include[0]"
    data["prompts"]["include"] = []
    data["milestones"][0]["acceptance"].append({"check": "rubric_grader",
                                                "params": {"rubric": "notes/extra.md"}})
    (pkg / "agent.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ManifestError) as exc:
        spec_hash(load_manifest(pkg))
    assert exc.value.path == "milestones[0].acceptance[2].params.rubric"
    with pytest.raises(ManifestError, match="no agent.yaml"):
        spec_description(tmp_path / "missing")
    with pytest.raises(ManifestError):
        spec_hash(tmp_path / "missing")
    with pytest.raises(ManifestError, match="package directory"):
        spec_hash(parse_manifest(copy.deepcopy(BASE)))


def test_operator_fields_feed_the_stamped_manifest(tmp_path):
    from agentkit.manifest import operator_fields, spec_hash

    data = copy.deepcopy(BASE)
    data["tools"] = ["write_file", "read_file", "submit_milestone"]
    data["listing"]["capabilities"] = ["Writes reports", "Cites sources", "Writes reports"]
    m = load_manifest(_package(tmp_path, manifest=data))
    fields = operator_fields(m)
    assert fields == {"model": "anthropic:claude-opus-5",
                      "tools": ["read_file", "submit_milestone", "write_file"],
                      "skills": ["Cites sources", "Writes reports"],
                      "mcp_servers": [], "spec_hash": spec_hash(m)}
    json.dumps(fields)   # plain JSON: strings and lists only
