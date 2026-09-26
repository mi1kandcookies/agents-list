"""
agentkit/manifest.py - strict parser for a specialist's agent.yaml (manifest v1).

agent.yaml is the specialist's private runtime spec: prompts, models, tools,
limits, egress, the human gate, intake and milestone templates. What a buyer
hires against is the platform's operator-stamped manifest (app/seller/stamp.py);
operator_fields() gives the values it takes from agent.yaml, including
spec_hash, so a stamp covers the whole runtime spec. Parsing is strict so a
typo never silently changes behavior:

    - unknown keys are rejected, and so are duplicate keys in the YAML
    - every value is type-checked (bool is not accepted as a number; NaN and
      infinity are not numbers)
    - milestone ids are short [A-Za-z0-9_-] names; deliverables are
      workspace-relative (no drive, root, UNC, "..", ":" or inputs/ and
      .agentkit/ prefixes)
    - a builtin check's kind is fixed: rubric_grader is "rubric" and
      human_signoff is "human" (inferred when kind is omitted); every other
      builtin is "automated" and cannot be marked rubric or human
    - errors raise ManifestError with a dotted path, e.g.
      "milestones[1].acceptance[0].check"

parse_milestone() applies the same rules to a milestone that arrives in a
Brief. YAML is read with a safe loader only. See
docs/decisions/0002-specialist-kit.md.

spec_hash(manifest) is "0x" + sha256 of the canonical JSON (agentkit.evidence
rules) of

    {"v": 1, "files": [{"path", "sha256", "bytes"}, ...]}

listing the package's runtime files: agent.yaml, every *.py in the package
and its subdirectories (except evals/), and every file under prompts/,
playbook/ and rubrics/. README, REFERENCES and evals/ are not runtime
files. Skipped everywhere, so a stray file on the stamping machine never
changes the hash: __pycache__, dotfiles and dot-directories (.DS_Store,
.git), and editor or OS leftovers (*~, #*#, *.swp, *.swo, *.pyc, *.pyo,
Thumbs.db, ehthumbs.db, desktop.ini). Paths are POSIX, relative to the
package and sorted; each file's bytes have CRLF turned into LF first
(sha256 and bytes are of the normalized content), so Windows and Linux
checkouts hash the same. A prompt or rubric the manifest points at outside
those files is an error.

The hash covers the specialist package, not the kit: agentkit itself (the
loop, the policy gate, the builtin checks, the kit's prompt rules) is pinned
by the deployment, so a kit upgrade can change how a stamped specialist
runs without changing its spec_hash.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import types as _pytypes
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from agentkit.errors import ManifestError
from agentkit.llm.base import PROVIDERS, ModelRef
from agentkit.types import HumanReview, MilestoneSpec

SCHEMA_VERSION = 1
SPEC_HASH_VERSION = 1
# Package folders whose every file is part of the runtime spec (spec_hash).
SPEC_TREES = ("prompts", "playbook", "rubrics")
# Left out of spec_hash wherever they appear (see the module docstring).
_SPEC_JUNK_NAMES = {"__pycache__", "thumbs.db", "ehthumbs.db", "desktop.ini"}
_SPEC_JUNK_SUFFIXES = ("~", ".swp", ".swo", ".pyc", ".pyo")
# The platform's default per-payment cap for x402 tasks (X402_MAX_PAYMENT_USDC).
TASK_PRICE_MAX_USDC = 10.0
Profile = Literal["code", "research", "docs", "data", "regulated-draft"]
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MILESTONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SPEC_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
# Top-level workspace folders a deliverable may not live in.
_NO_DELIVERABLES_IN = ("inputs", ".agentkit")


@dataclass
class Pricing:
    model: Literal["per_milestone", "fixed", "hourly"] = "per_milestone"
    currency: str = "USDC"
    typical_low: float = 0.0
    typical_high: float = 0.0
    # The flat price of one paid agent-to-agent task (x402), which the
    # operator stamps as the platform manifest's price. Separate from the
    # engagement range above and small: 0 < x <= TASK_PRICE_MAX_USDC.
    task_price_usdc: float | None = None


@dataclass
class Listing:
    category: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    pricing: Pricing = field(default_factory=Pricing)


@dataclass
class Models:
    primary: str = "anthropic:claude-opus-5"
    fallbacks: list[str] = field(default_factory=list)
    grader: str = "anthropic:claude-sonnet-5"
    # Adapter options per provider, applied to every adapter of that provider
    # (primary, fallbacks, grader), e.g. {anthropic: {effort: xhigh}}. See the
    # option lists in agentkit/llm/anthropic.py and openai_compat.py.
    options: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Prompts:
    system: str = "prompts/system.md"
    include: list[str] = field(default_factory=list)


@dataclass
class ShellConfig:
    allow: list[str] = field(default_factory=list)
    timeout_seconds: int = 600


@dataclass
class EgressConfig:
    """Network access. With intake_field set (mode allowlist only), the
    Brief's intake value under that name - a list of hostnames - either
    narrows the allowlist to those hosts (intake_mode "narrow": a host must
    match both lists) or extends it with exact hostnames (intake_mode
    "extend": no "*" or wildcard rules). Without intake_field the intake
    never changes egress."""
    mode: Literal["none", "allowlist"] = "none"
    allow: list[str] = field(default_factory=list)
    intake_field: str = ""
    intake_mode: Literal["narrow", "extend"] = "narrow"


@dataclass
class Limits:
    max_steps: int = 80
    max_tokens: int = 3_000_000
    max_usd: float = 40.0
    max_wall_minutes: float = 180.0


@dataclass
class HumanGate:
    required: bool = False
    reviewer_role: str = ""
    disclaimer: str = ""
    checklist: list[str] = field(default_factory=list)


@dataclass
class IntakeField:
    field: str
    question: str
    required: bool = True


@dataclass
class EstimateConfig:
    usd_per_hour: float = 6.0


@dataclass
class Manifest:
    schema_version: int
    slug: str
    name: str
    version: str
    summary: str
    listing: Listing
    profile: Profile
    milestones: list[MilestoneSpec]
    models: Models = field(default_factory=Models)
    prompts: Prompts = field(default_factory=Prompts)
    tools: list[str] = field(default_factory=list)
    shell: ShellConfig = field(default_factory=ShellConfig)
    egress: EgressConfig = field(default_factory=EgressConfig)
    limits: Limits = field(default_factory=Limits)
    human_gate: HumanGate = field(default_factory=HumanGate)
    intake: list[IntakeField] = field(default_factory=list)
    estimate: EstimateConfig = field(default_factory=EstimateConfig)
    # Directory holding agent.yaml; not part of the YAML. Relative prompt,
    # playbook and rubric paths resolve against it.
    base_dir: Path | None = field(default=None, compare=False)

    @property
    def package(self) -> str:
        return self.slug.replace("-", "_")

    def milestone(self, milestone_id: str) -> MilestoneSpec | None:
        return next((m for m in self.milestones if m.id == milestone_id), None)

    def human_review(self) -> HumanReview:
        g = self.human_gate
        return HumanReview(required=g.required, reviewer_role=g.reviewer_role,
                           checklist=list(g.checklist), disclaimer=g.disclaimer)

    def resolve(self, relative: str) -> Path:
        """A path from the manifest (prompt, rubric) resolved against base_dir."""
        return (self.base_dir or Path(".")) / relative

    def public_listing(self) -> dict[str, Any]:
        """The catalog / agent-registration view: what a buyer or another
        agent may see. Excludes prompts, tools, limits and model choices."""
        lst = self.listing
        return {
            "schema_version": self.schema_version,
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "category": lst.category,
            "description": lst.description,
            "tags": list(lst.tags),
            "capabilities": list(lst.capabilities),
            "pricing": {"model": lst.pricing.model, "currency": lst.pricing.currency,
                        "typical_low": lst.pricing.typical_low,
                        "typical_high": lst.pricing.typical_high,
                        "task_price_usdc": lst.pricing.task_price_usdc},
            "profile": self.profile,
            "human_review": {"required": self.human_gate.required,
                             "reviewer_role": self.human_gate.reviewer_role},
            "intake": [{"field": f.field, "question": f.question, "required": f.required}
                       for f in self.intake],
            "milestones": [{"id": m.id, "title": m.title, "description": m.description,
                            "deliverables": list(m.deliverables),
                            "hours": [m.hours[0], m.hours[1]]}
                           for m in self.milestones],
        }


# --- generic strict parsing ---------------------------------------------------

def _type_name(tp: Any) -> str:
    return getattr(tp, "__name__", None) or str(tp)


def _coerce(tp: Any, value: Any, path: str) -> Any:
    origin = get_origin(tp)
    if tp is Any:
        return value
    if origin in (Union, _pytypes.UnionType):
        args = get_args(tp)
        if value is None and type(None) in args:
            return None
        inner = [a for a in args if a is not type(None)]
        return _coerce(inner[0], value, path)
    if origin is Literal:
        if value not in get_args(tp):
            allowed = ", ".join(repr(a) for a in get_args(tp))
            raise ManifestError(f"must be one of {allowed}, got {value!r}", path)
        return value
    if origin is list:
        if not isinstance(value, list):
            raise ManifestError(f"must be a list, got {type(value).__name__}", path)
        (item,) = get_args(tp) or (Any,)
        return [_coerce(item, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ManifestError(f"must be a mapping, got {type(value).__name__}", path)
        for k in value:
            if not isinstance(k, str):
                raise ManifestError(f"keys must be strings, got {k!r}", path)
        _, item = get_args(tp) or (str, Any)
        return {k: _coerce(item, v, f"{path}.{k}") for k, v in value.items()}
    if origin is tuple:
        args = get_args(tp)
        if not isinstance(value, (list, tuple)) or len(value) != len(args):
            raise ManifestError(f"must be a list of {len(args)} values", path)
        return tuple(_coerce(a, v, f"{path}[{i}]") for i, (a, v) in enumerate(zip(args, value)))
    if is_dataclass(tp):
        return _parse(tp, value, path)
    if tp is bool:
        if not isinstance(value, bool):
            raise ManifestError(f"must be true or false, got {value!r}", path)
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError(f"must be an integer, got {value!r}", path)
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ManifestError(f"must be a number, got {value!r}", path)
        if not math.isfinite(value):
            raise ManifestError(f"must be a finite number, got {value!r}", path)
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ManifestError(f"must be a string, got {type(value).__name__}", path)
        return value
    raise ManifestError(f"unsupported type {_type_name(tp)}", path)  # pragma: no cover


def _parse(cls: Any, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ManifestError(f"must be a mapping, got {type(data).__name__}", path or "<root>")
    hints = get_type_hints(cls, vars(sys.modules[cls.__module__]))
    specs = {f.name: f for f in fields(cls) if f.name != "base_dir"}
    prefix = f"{path}." if path else ""
    for key in data:
        if key not in specs:
            raise ManifestError(f"unknown key (expected one of {', '.join(specs)})", f"{prefix}{key}")
    kwargs = {}
    for name, spec in specs.items():
        if name in data:
            kwargs[name] = _coerce(hints[name], data[name], f"{prefix}{name}")
        elif spec.default is MISSING and spec.default_factory is MISSING:  # type: ignore[misc]
            raise ManifestError("is required", f"{prefix}{name}")
    return cls(**kwargs)


# --- semantic validation ----------------------------------------------------------

def _validate(m: Manifest) -> None:
    if m.schema_version != SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema_version (this kit reads {SCHEMA_VERSION})",
                            "schema_version")
    if not _SLUG_RE.match(m.slug):
        raise ManifestError("must be lowercase kebab-case", "slug")
    for key in ("name", "version", "summary"):
        if not getattr(m, key).strip():
            raise ManifestError("must not be empty", key)
    task_price = m.listing.pricing.task_price_usdc
    if task_price is not None:
        if not 0 < task_price <= TASK_PRICE_MAX_USDC:
            raise ManifestError(f"must be > 0 and <= {TASK_PRICE_MAX_USDC:g}",
                                "listing.pricing.task_price_usdc")
        if round(task_price, 6) != task_price:
            raise ManifestError("must have at most 6 decimals (micro-USDC)",
                                "listing.pricing.task_price_usdc")
    for key in ("primary", "grader"):
        _check_ref(getattr(m.models, key), f"models.{key}")
    for i, ref in enumerate(m.models.fallbacks):
        _check_ref(ref, f"models.fallbacks[{i}]")
    for provider in m.models.options:
        if provider not in PROVIDERS or provider == "scripted":
            raise ManifestError(f"unknown provider {provider!r}", f"models.options.{provider}")
    for i, name in enumerate(m.tools):
        if not _TOOL_RE.match(name):
            raise ManifestError(f"invalid tool name {name!r}", f"tools[{i}]")
    if len(set(m.tools)) != len(m.tools):
        raise ManifestError("duplicate tool names", "tools")
    if m.shell.timeout_seconds <= 0:
        raise ManifestError("must be positive", "shell.timeout_seconds")
    if m.egress.mode == "none" and m.egress.allow:
        raise ManifestError("must be empty when egress.mode is 'none'", "egress.allow")
    if m.egress.intake_field:
        if m.egress.mode != "allowlist":
            raise ManifestError("needs egress.mode 'allowlist'", "egress.intake_field")
        if m.egress.intake_field not in {f.field for f in m.intake}:
            raise ManifestError(f"{m.egress.intake_field!r} is not an intake field", "egress.intake_field")
    for key in ("max_steps", "max_tokens", "max_usd", "max_wall_minutes"):
        if getattr(m.limits, key) <= 0:
            raise ManifestError("must be positive", f"limits.{key}")
    if m.human_gate.required and not m.human_gate.reviewer_role.strip():
        raise ManifestError("is required when human_gate.required is true",
                            "human_gate.reviewer_role")
    if not m.milestones:
        raise ManifestError("at least one milestone is required", "milestones")
    seen: set[str] = set()
    for i, ms in enumerate(m.milestones):
        p = f"milestones[{i}]"
        if ms.id in seen:
            raise ManifestError(f"duplicate milestone id {ms.id!r}", f"{p}.id")
        seen.add(ms.id)
        validate_milestone(ms, p)


def check_relative_path(value: str, path: str) -> None:
    """A workspace-relative path that stays inside the workspace on every OS."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ManifestError("must be a non-empty path", path)
    parts = re.split(r"[\\/]+", value)
    win = PureWindowsPath(value)
    if (PurePosixPath(value).is_absolute() or win.drive or win.root or ":" in value
            or ".." in parts):
        raise ManifestError("must be a workspace-relative path (no drive, root, '..' or ':')", path)
    if parts[0].lower() in _NO_DELIVERABLES_IN:
        raise ManifestError(f"must not be under {parts[0]}/", path)


def validate_milestone(ms: MilestoneSpec, path: str) -> None:
    """Semantic rules for one milestone (manifest or brief). May fill in the
    kind of rubric_grader / human_signoff criteria that left it at the default."""
    if not _MILESTONE_ID_RE.match(ms.id or ""):
        raise ManifestError("must be 1-64 characters of [A-Za-z0-9_-], starting with a letter "
                            "or digit", f"{path}.id")
    if not (ms.title or "").strip():
        raise ManifestError("must not be empty", f"{path}.title")
    lo, hi = ms.hours
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo < 0 or hi < lo:
        raise ManifestError("must be [low, high] with 0 <= low <= high", f"{path}.hours")
    for j, d in enumerate(ms.deliverables):
        check_relative_path(d, f"{path}.deliverables[{j}]")
    from agentkit.checks.builtin import BUILTIN_KINDS

    for j, ac in enumerate(ms.acceptance):
        where = f"{path}.acceptance[{j}]"
        if not ac.check.strip():
            raise ManifestError("must not be empty", f"{where}.check")
        declared = BUILTIN_KINDS.get(ac.check)
        if declared is None or ac.kind == declared:
            continue
        if ac.kind == "automated" and declared != "automated":
            ac.kind = declared  # left at the default: take the check's own kind
            continue
        raise ManifestError(f"{ac.check} is a {declared} check and cannot be marked {ac.kind!r}",
                            f"{where}.kind")


def parse_milestone(data: Any, path: str = "milestone") -> MilestoneSpec:
    """Strictly parse and validate one milestone mapping (e.g. from a Brief)."""
    ms = _parse(MilestoneSpec, data, path)
    validate_milestone(ms, path)
    return ms


def _check_ref(ref: str, path: str) -> None:
    try:
        ModelRef.parse(ref)
    except ValueError as exc:
        raise ManifestError(str(exc), path) from None


def parse_manifest(data: Any, *, base_dir: Path | None = None) -> Manifest:
    """Parse and validate an already-loaded manifest mapping."""
    manifest = _parse(Manifest, data, "")
    manifest.base_dir = base_dir
    _validate(manifest)
    return manifest


def load_yaml(text: str) -> Any:
    """yaml.safe_load, except that a mapping with a duplicate key is an
    error (safe_load silently keeps the last one)."""
    import yaml  # PyYAML; safe loader only

    class _StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> Any:
        loader.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                continue  # unhashable key: construct_mapping reports it
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass


def load_manifest(path: str | Path, *, check_files: bool = True) -> Manifest:
    """Read agent.yaml (a file, or a directory containing one)."""
    import yaml

    path = Path(path)
    if path.is_dir():
        path = path / "agent.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from None
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML: {exc}") from None
    manifest = parse_manifest(data, base_dir=path.parent)
    if check_files:
        files = [("prompts.system", manifest.prompts.system)]
        files += [(f"prompts.include[{i}]", p) for i, p in enumerate(manifest.prompts.include)]
        for where, rel in files:
            if not manifest.resolve(rel).is_file():
                raise ManifestError(f"file not found: {rel}", where)
    return manifest


# --- bridge to the operator-stamped platform manifest ------------------------------

def _package_dir(source: "Manifest | str | Path") -> Path:
    if isinstance(source, Manifest):
        if source.base_dir is None:
            raise ManifestError("the manifest has no package directory (it was not loaded from a file)")
        return Path(source.base_dir)
    path = Path(source)
    return path.parent if path.is_file() else path


def _spec_junk(name: str) -> bool:
    low = name.lower()
    return (name.startswith(".") or (name.startswith("#") and name.endswith("#"))
            or low in _SPEC_JUNK_NAMES or low.endswith(_SPEC_JUNK_SUFFIXES))


def spec_files(source: "Manifest | str | Path") -> list[tuple[str, bytes]]:
    """(relative POSIX path, bytes with CRLF -> LF) of every runtime file of
    a specialist package, sorted by path. `source` is a loaded Manifest, the
    package directory or its agent.yaml."""
    base = _package_dir(source)
    if not (base / "agent.yaml").is_file():
        raise ManifestError(f"no agent.yaml in {base}")
    rels = {"agent.yaml"}
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath).relative_to(base)
        dirnames[:] = [d for d in dirnames if not _spec_junk(d)
                       and not (here.parts == () and d in ("evals", *SPEC_TREES))]
        rels.update((here / name).as_posix() for name in filenames
                    if name.endswith(".py") and not _spec_junk(name))
    for tree in SPEC_TREES:
        top = base / tree
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if not _spec_junk(d)]
            rels.update((Path(dirpath) / name).relative_to(base).as_posix()
                        for name in filenames if not _spec_junk(name))
    return [(rel, (base / rel).read_bytes().replace(b"\r\n", b"\n")) for rel in sorted(rels)]


def spec_description(source: "Manifest | str | Path") -> dict[str, Any]:
    """What spec_hash hashes: {"v": 1, "files": [{"path", "sha256", "bytes"}]}."""
    return {"v": SPEC_HASH_VERSION,
            "files": [{"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                      for rel, data in spec_files(source)]}


def _check_spec_covers(manifest: Manifest, files: list[str]) -> None:
    """Every prompt and rubric the manifest loads must be one of the hashed files."""
    base = _package_dir(manifest)
    hashed = {(base / rel).resolve() for rel in files}
    refs = [("prompts.system", manifest.prompts.system)]
    refs += [(f"prompts.include[{i}]", p) for i, p in enumerate(manifest.prompts.include)]
    for i, ms in enumerate(manifest.milestones):
        for j, ac in enumerate(ms.acceptance):
            rubric = ac.params.get("rubric")
            if ac.check == "rubric_grader" and isinstance(rubric, str):
                refs.append((f"milestones[{i}].acceptance[{j}].params.rubric", rubric))
    for where, rel in refs:
        if (base / rel).resolve() not in hashed:
            raise ManifestError(f"{rel} is outside the files spec_hash covers "
                                f"(agent.yaml, *.py, {', '.join(t + '/' for t in SPEC_TREES)})", where)


def spec_hash(source: "Manifest | str | Path") -> str:
    """ "0x" + sha256 of the canonical spec description (see the module docstring)."""
    from agentkit.evidence import canonical_json

    manifest = source if isinstance(source, Manifest) else load_manifest(_package_dir(source))
    description = spec_description(manifest)
    _check_spec_covers(manifest, [f["path"] for f in description["files"]])
    return "0x" + hashlib.sha256(canonical_json(description)).hexdigest()


def verify_spec_hash(source: "Manifest | str | Path", expected: str) -> str:
    """Raise ManifestError unless the package hashes to `expected` (the
    spec_hash of the operator-stamped manifest; hex case is ignored).
    Reads the package's files and imports none of its code, so a caller can
    check before loading it. Returns the hash."""
    want = expected.strip().lower() if isinstance(expected, str) else ""
    if not _SPEC_HASH_RE.match(want):
        raise ManifestError(f"spec hash must be 0x followed by 64 hex digits, got {expected!r}")
    actual = spec_hash(source)
    if actual != want:
        raise ManifestError(f"spec hash mismatch: expected {want}, this package hashes to {actual}")
    return actual


def task_price_micro(manifest: Manifest) -> int | None:
    """listing.pricing.task_price_usdc as exact integer micro-USDC (None when
    the listing sets no task price): what the operator stamps as
    price_min_micro and price_max_micro. Converted through Decimal, since
    the float product is not exact (4.1 * 10**6 is 4099999.999...)."""
    price = manifest.listing.pricing.task_price_usdc
    if price is None:
        return None
    micro = Decimal(repr(float(price))) * 1_000_000
    if micro != micro.to_integral_value():   # parse_manifest already refuses this
        raise ManifestError("must have at most 6 decimals (micro-USDC)",
                            "listing.pricing.task_price_usdc")
    return int(micro)


def operator_fields(manifest: Manifest) -> dict[str, Any]:
    """The values the operator-stamped platform manifest takes from agent.yaml
    (app.seller.stamp.build_manifest adds the agent id, price and payout
    address): the primary model ref, the tool names, the listing's
    capabilities as skills, no MCP servers (v1 specialists use none) and
    spec_hash. Tools and skills come sorted and de-duplicated, as the
    platform stores them. Pure: reads the package files, imports no app code."""
    return {
        "model": manifest.models.primary,
        "tools": sorted(set(manifest.tools)),
        "skills": sorted(set(manifest.listing.capabilities)),
        "mcp_servers": [],
        "spec_hash": spec_hash(manifest),
    }


__all__ = ["EgressConfig", "EstimateConfig", "HumanGate", "IntakeField", "Limits", "Listing",
           "Manifest", "Models", "Pricing", "Prompts", "SCHEMA_VERSION", "SPEC_HASH_VERSION",
           "SPEC_TREES", "ShellConfig", "TASK_PRICE_MAX_USDC", "check_relative_path",
           "load_manifest", "load_yaml", "operator_fields", "parse_manifest", "parse_milestone",
           "spec_description", "spec_files", "spec_hash", "task_price_micro", "validate_milestone",
           "verify_spec_hash"]
