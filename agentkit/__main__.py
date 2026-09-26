"""
agentkit/__main__.py - the harness CLI.

    python -m agentkit list
    python -m agentkit show SLUG
    python -m agentkit validate SLUG
    python -m agentkit validate-intake SLUG --intake intake.json
    python -m agentkit estimate SLUG [--intake intake.json]
    python -m agentkit milestones SLUG [--intake intake.json]
    python -m agentkit spec-hash SLUG [--files]
    python -m agentkit run SLUG --brief brief.json --milestone ID --workspace DIR
                                [--milestone-idx N] [--spec-hash 0x...] [--model REF]
                                [--resume] [--no-grader]
    python -m agentkit check SLUG --milestone ID --workspace DIR [--brief brief.json]
    python -m agentkit eval SLUG --out DIR [--case NAME] [--model REF]

`run` prints JSON-line events on stdout and writes the Submission to
<workspace>/.agentkit/submissions/<milestone>.json; --milestone-idx is the
platform's SOW milestone index (default: the milestone's position in the
brief, else in the manifest), which the evidence hash binds. With
--spec-hash (the spec_hash in the operator-stamped manifest) it refuses to
run a package whose runtime files hash differently, checked before any of
the package's code is imported. `spec-hash` prints that hash (--files: the
hashed description as JSON).

Run the CLI from outside the workspace (e.g. the repo root): `python -m`
puts the working directory first on sys.path, so files the agent writes
there could otherwise shadow modules the kit imports. run, check and eval
refuse a working directory inside their workspace (or eval's --out), and a
specialist package that would be loaded from anywhere but its own
directory (see agentkit.registry).

Models come from the manifest (models.primary, fallbacks, grader), so a
production run uses the model the operator stamped. For development only,
AGENTKIT_ALLOW_MODEL_OVERRIDE=1 enables overrides: --model > AGENTKIT_MODEL >
manifest; fallbacks from AGENTKIT_FALLBACK_MODELS; grader from
AGENTKIT_GRADER_MODEL. Without it any override given is ignored, with a
warning on stderr. Per-provider adapter options come from the manifest's
models.options.

Exit codes:

    0   success; for run, the submission is ready_for_review
    1   check: the checks would not give ready_for_review;
        validate / validate-intake: problems or blocking gaps were found
    2   usage or configuration error: unknown specialist or milestone, a
        malformed brief, manifest, rubric or intake file, a specialist
        whose wiring fails validate, a --spec-hash that does not match, or
        a working directory inside the workspace (nothing was run)
    3   run: a submission was written but it is not ready_for_review
        (needs_revision, incomplete - including model errors - or
        budget_exceeded); see its status and the events

`list` skips broken manifests and names each one on stderr.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from agentkit.errors import AgentKitError
from agentkit.events import JsonlSink
from agentkit.loop import RunOutcome
from agentkit.manifest import spec_description, spec_hash
from agentkit.registry import DEFAULT_PACKAGE, list_specialists, load_specialist
from agentkit.security import secrets_from_env
from agentkit.specialist import (ALLOW_MODEL_OVERRIDE_ENV, RunContext, Specialist, build_adapter,
                                 ignored_model_overrides, resolve_model_refs)
from agentkit.types import Brief

# (primary, fallbacks, env) -> adapter; tests replace it with a scripted one.
AdapterFactory = Callable[[str, list[str], dict[str, str]], Any]


def _default_factory(primary: str, fallbacks: list[str], env: dict[str, str], *,
                     options: dict[str, dict[str, Any]] | None = None) -> Any:
    return build_adapter(primary, fallbacks, env=env, options=options)


def _json(out: TextIO, data: Any) -> None:
    out.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _read_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_brief(path: str) -> Brief:
    """A Brief from a JSON file; a malformed one is a usage error (exit 2)."""
    data = _read_json(path)
    try:
        if not isinstance(data, dict):
            raise TypeError("the brief must be a JSON object")
        return Brief.from_dict(data)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        detail = f"missing field {exc}" if isinstance(exc, KeyError) else str(exc)
        raise AgentKitError(f"invalid brief {path}: {detail}") from None


def _check_cwd(workspace: str) -> None:
    """Refuse to work on a workspace that contains the working directory."""
    ws, cwd = Path(workspace).resolve(), Path.cwd().resolve()
    if cwd == ws or ws in cwd.parents:
        raise AgentKitError(f"the working directory {cwd} is inside the workspace {ws}; run the "
                            "harness from outside it (e.g. the repo root), since python imports "
                            "from the working directory first")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agentkit", description="Specialist agent harness")
    p.add_argument("--root", help=argparse.SUPPRESS)
    p.add_argument("--package", default=DEFAULT_PACKAGE, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list specialists")
    val = sub.add_parser("validate", help="check a specialist's tools, checks and rubrics")
    val.add_argument("slug")
    for name in ("show", "validate-intake", "estimate", "milestones"):
        sp = sub.add_parser(name)
        sp.add_argument("slug")
        sp.add_argument("--intake", help="intake JSON file")
    sh = sub.add_parser("spec-hash", help="print the hash of a specialist's runtime files")
    sh.add_argument("slug")
    sh.add_argument("--files", action="store_true", help="print the hashed description as JSON")
    run = sub.add_parser("run", help="run one milestone")
    run.add_argument("slug")
    run.add_argument("--brief", required=True)
    run.add_argument("--milestone", required=True)
    run.add_argument("--workspace", required=True)
    run.add_argument("--milestone-idx", type=int, help="the platform's SOW milestone index")
    run.add_argument("--spec-hash", help="refuse to run unless the package hashes to this value")
    run.add_argument("--model")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--no-grader", action="store_true")
    chk = sub.add_parser("check", help="re-run a milestone's acceptance checks")
    chk.add_argument("slug")
    chk.add_argument("--milestone", required=True)
    chk.add_argument("--workspace", required=True)
    chk.add_argument("--brief")
    chk.add_argument("--grader", action="store_true", help="use the grader model for rubric checks")
    ev = sub.add_parser("eval", help="run golden cases against a live model")
    ev.add_argument("slug")
    ev.add_argument("--out", required=True)
    ev.add_argument("--case")
    ev.add_argument("--model")
    return p


def _grader(spec: Specialist, grader_ref: str, env: dict[str, str], factory: AdapterFactory,
           out: TextIO | None) -> Any:
    try:
        return factory(grader_ref, [], env)
    except Exception as exc:  # no SDK or key: rubric checks stay pending
        if out is not None:
            out.write(json.dumps({"type": "warning", "data": {"grader": grader_ref, "error": str(exc)}}) + "\n")
        return None


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None,
         env: dict[str, str] | None = None, adapter_factory: AdapterFactory | None = None) -> int:
    out = stdout or sys.stdout
    env = dict(os.environ if env is None else env)
    args = _parser().parse_args(argv)
    root = Path(args.root) if args.root else None
    package = args.package or None
    try:
        if args.command in ("run", "check", "eval"):
            _check_cwd(args.out if args.command == "eval" else args.workspace)
        if args.command == "list":
            errors: list[tuple[Path, str]] = []
            for m in list_specialists(root, errors=errors):
                out.write(f"{m.slug:24} {m.listing.category:18} {m.profile:16} "
                          f"{'human-gated' if m.human_gate.required else '-':12} {m.name}\n")
            for path, message in errors:
                sys.stderr.write(f"warning: skipped {path}: {message}\n")
            return 0
        spec = load_specialist(args.slug, root=root, package=package,
                               spec_hash=getattr(args, "spec_hash", None))
        m = spec.manifest
        factory = adapter_factory or functools.partial(_default_factory, options=m.models.options)
        if args.command == "validate":
            problems = spec.validate()
            _json(out, problems)
            return 1 if problems else 0
        if args.command == "show":
            _json(out, {**m.public_listing(), "tools": m.tools, "models": {
                "primary": m.models.primary, "fallbacks": m.models.fallbacks, "grader": m.models.grader}})
            return 0
        if args.command == "validate-intake":
            missing = spec.validate_intake(_read_json(args.intake))
            _json(out, [x.to_dict() for x in missing])
            return 1 if any(x.blocking for x in missing) else 0
        if args.command == "estimate":
            _json(out, spec.estimate(_read_json(args.intake)).to_dict())
            return 0
        if args.command == "milestones":
            _json(out, [x.to_dict() for x in spec.propose_milestones(_read_json(args.intake))])
            return 0
        if args.command == "spec-hash":
            digest = spec_hash(m)
            if args.files:
                _json(out, {"spec_hash": digest, "description": spec_description(m)})
            else:
                out.write(digest + "\n")
            return 0
        model = getattr(args, "model", None)
        ignored = ignored_model_overrides(model=model, env=env)
        if ignored:
            sys.stderr.write(f"warning: ignoring {', '.join(ignored)}: model overrides need "
                             f"{ALLOW_MODEL_OVERRIDE_ENV}=1 (development only); using the "
                             "manifest's models\n")
        primary, fallbacks, grader_ref = resolve_model_refs(m, model=model, env=env)
        if args.command == "run":
            brief = _load_brief(args.brief)
            events = JsonlSink(out, secrets=secrets_from_env(env))
            ctx = RunContext(brief=brief, workspace=Path(args.workspace),
                             adapter=factory(primary, fallbacks, env),
                             grader=None if args.no_grader else _grader(spec, grader_ref, env, factory, out),
                             events=events, env=env, resume=args.resume,
                             milestone_idx=args.milestone_idx)
            sub = spec.run_milestone(ctx, args.milestone)
            return 0 if sub.status == "ready_for_review" else 3
        if args.command == "check":
            brief = _load_brief(args.brief) if args.brief else \
                Brief(engagement_id="", specialist=m.slug, objective="")
            grader = _grader(spec, grader_ref, env, factory, out) if args.grader else None
            ctx = RunContext(brief=brief, workspace=Path(args.workspace), grader=grader, env=env)
            results = spec.check(Path(args.workspace), args.milestone, ctx)
            _json(out, [r.to_dict() for r in results])
            # the same rule a submission's status uses (a failed rubric counts)
            ready = Specialist.status_for(RunOutcome(status="submitted"), results) == "ready_for_review"
            return 0 if ready else 1
        if args.command == "eval":
            from agentkit.evals import run_evals
            results = run_evals(spec, adapter=factory(primary, fallbacks, env), out_dir=Path(args.out),
                                grader=_grader(spec, grader_ref, env, factory, out), only=args.case)
            _json(out, results)
            return 0
    except (AgentKitError, ImportError, ValueError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 2  # pragma: no cover - argparse enforces a command


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
