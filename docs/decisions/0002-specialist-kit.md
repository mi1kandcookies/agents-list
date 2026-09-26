# 0002 - Specialist kit (`agentkit`)

Status: proposed · Date: 2026-09-26

## Context

Today an "agent" on Agent's List is a catalog row with an operator-stamped
manifest (decision 0001): nothing on the platform runs it. Before onboarding
vendor agents we want first-party specialists that do real, milestone-based
work on their own VMs. Ten of them share the same needs: a model-agnostic
tool loop, sandboxed tools, grounded claims, acceptance checks, milestone
evidence the platform can verify, and a harness entry point. Building those
ten times would drift, so they share one kit. The kit proposes the private
part of the manifest (#9) and the in-VM runner that the harness (#11) and
orchestrator (#10) wrap; those issues stay open for the platform side.

## Decision

Two Flask-free top-level packages, mirroring `chain/`:

- `agentkit/` - the shared, provider-neutral kit.
- `specialists/<package>/` - one package per agent: `agent.yaml`, `prompts/`,
  `playbook/`, `rubrics/`, `tools.py`, `checks.py`, `evals/`, and optionally
  `agent.py` (a `Specialist` subclass that overrides hooks such as
  `finalize`; without it the registry uses the plain base class).

### Contract (v1)

| Module | Responsibility |
|---|---|
| `types.py` | JSON-round-trippable dataclasses: `Message`, `ToolCall`, `ToolResult`, `ModelResponse`, `Usage`, `Brief`, `MilestoneSpec`, `AcceptanceCriterion`, `CheckResult`, `HumanReview`, `Artifact`, `Submission`, ... |
| `errors.py` | `AgentKitError` and its subclasses (`ManifestError`, `ModelError`, `BudgetExceeded`, `PolicyViolation`, `ToolError`) |
| `manifest.py` | strict parse of `agent.yaml` (unknown keys rejected, dotted error paths); `operator_fields()` and `spec_hash()` for the stamped manifest; `public_listing()` for the catalog |
| `llm/` | `ModelAdapter` protocol, `"provider:model"` refs, adapters built on the official vendor SDKs, `ScriptedAdapter` for offline tests, fallback chain |
| `security.py`, `policy.py` | untrusted-content wrapping, redaction, host matching; `PolicyGate` (tool, shell, egress, workspace-write rules) |
| `events.py`, `journal.py`, `ledger.py` | event stream, append-only journal with per-milestone checkpoints, claim ledger with verbatim-quote verification |
| `tools/` | `Tool`, `ToolContext` (`fetch` egress-checked, `run` shell-checked), `ToolRegistry`; workspace, shell, web, documents, ledger and platform tools |
| `loop.py` | `Runner`: think, act, observe; append-only history; one tool message per turn; limits raise `BudgetExceeded`; stuck detector; one nudge; resume from checkpoint |
| `checks/`, `evidence.py` | acceptance-check registry and builtins; the platform's canonical JSON, milestone evidence text and its hash |
| `specialist.py`, `registry.py`, `evals.py`, `__main__.py` | `Specialist` base and `RunContext`, discovery, live evals, `python -m agentkit` |

### Manifest: private runtime spec, public hire gate

`agent.yaml` is the specialist's private runtime spec, the private part of
#9: prompts, models, tools, shell and egress rules, limits, the human gate,
intake questions and milestone templates with structured acceptance checks.
It is not what a buyer hires against. The hire gate stays the platform's
operator-stamped manifest (`app/seller/stamp.py`, decision 0001):
`{v, agent_id, model, tools, mcp_servers, skills, price_min_micro,
price_max_micro, payout_address}`, stamped by a verified operator and checked
by `assert_hireable()` on every hire path.

The bridge is `agentkit.manifest.operator_fields(manifest)`, which returns
the stamped manifest's values that come from `agent.yaml`:

| Field | From `agent.yaml` |
|---|---|
| `model` | `models.primary` |
| `tools` | `tools` (sorted, de-duplicated) |
| `skills` | `listing.capabilities` (sorted, de-duplicated) |
| `mcp_servers` | `[]`: v1 specialists use none |
| `spec_hash` | `"0x"` + sha256 of the package's runtime files (below) |

`spec_hash` covers `agent.yaml`, every `*.py` in the package and its
subdirectories (except `evals/`) and every file under `prompts/`,
`playbook/` and `rubrics/`: the canonical JSON of
`{"v": 1, "files": [{"path", "sha256", "bytes"}]}` with sorted POSIX paths
and CRLF normalized to LF, so Windows and Linux checkouts agree. README,
REFERENCES and `evals/` are left out, and so are `__pycache__`, dotfiles and
editor or OS leftovers (`*~`, `*.swp`, `Thumbs.db`, `desktop.ini`, ...), so a
stray file on the stamping machine does not change the hash. A prompt or
rubric outside the hashed files is an error. The hash covers the specialist
package, not the kit: `agentkit` itself (loop, policy gate, builtin checks,
the kit's prompt rules) is pinned by the deployment, so a kit upgrade can
change how a stamped specialist runs without changing its `spec_hash`.

Adding `spec_hash` as an optional key of the stamped manifest is a proposed
amendment to decision 0001; with it, a stamp covers the prompts, limits,
egress and human gate as well, and any edit to them needs a re-stamp.
`python -m agentkit spec-hash SLUG` prints the value, and
`run --spec-hash 0x...` refuses to run (exit 2) a package that hashes
differently, before any of its code is imported. The registry also refuses
a package that Python would import from anywhere but the hashed directory
(a same-named package earlier on `sys.path`), so the code that runs is the
code that was hashed.

The operator (or a seeding command) builds the stamped manifest with
`build_manifest(agent, **operator_fields minus spec_hash, price, payout)`.
The price is the flat x402 per-task price, `listing.pricing.task_price_usdc`
(0 < x <= 10 USDC, the platform's default payment cap), stamped as
`price_min_micro = price_max_micro = task_price_micro(manifest)`, the exact
integer micro-USDC (never `int(x * 10**6)` on the float, which turns 4.1
into 4099999). It is separate from the listing's engagement range
(`typical_low`/`typical_high`, billed per milestone through the SOW).

A production run uses the manifest's models, so it runs the stamped model
and moves to another model only through fallbacks that `agent.yaml` lists or
opts into (the first-party specialists use none); model overrides are
development only (see Model defaults).

### Platform plug-in points

The kit imports nothing from `app/`; a harness connects it to the platform
through the existing API:

- **Scope to SOW.** `propose_milestones(intake)` returns milestones in
  order; SOW milestone `idx` i is milestone i. Each becomes
  `{title, acceptance, amount_micro}` (or `amount_usdc`) for
  `POST /api/engagements`; MCP `request_scope` takes `amount_usdc` only, so
  format it from the integer micro-USDC with `Decimal` (e.g.
  `str(Decimal(micro) / 10**6)`), never through a float. `title` at most 200
  characters, `acceptance` the criteria as one `- ` line each (non-empty, at
  most 2000 characters), at most 20 milestones, every amount a positive
  integer of micro-USDC, and the amounts summing exactly to the budget's
  `budget_micro` or the platform answers `MILESTONES_MISMATCH`
  (`app/engagements/sow.py`). Split the budget by each milestone's share of
  `hours` in integer micro-USDC and let the last milestone absorb the
  rounding. `estimate()` can prefill amounts and a day range; the platform's
  rule-based estimate (`app/intake/estimate.py`) stays until a scoping agent
  replaces it.
- **Brief.** A harness builds the `Brief` from `GET /api/engagements/<id>`:
  `engagement_id` from the id, `objective` from `outcome`, `milestones` from
  `agent.yaml` in SOW order (the SOW keeps only titles and free-text
  acceptance, so the structured checks come from the spec), `sow_text` from
  the SOW. The platform does not store intake answers yet, so
  `Brief.intake` has no platform source. The `mandate_token` stays with the
  harness; v1 specialists do not use it.
- **Run.** `python -m agentkit run SLUG --brief brief.json --milestone ID
  --milestone-idx IDX --workspace DIR --spec-hash <stamped spec_hash>`,
  from the repo root or another directory outside the workspace: `python -m`
  imports from the working directory first, so `run`, `check` and `eval`
  refuse (exit 2) a working directory inside their workspace.
- **Submit.** The harness posts `platform_evidence(submission)` as
  `{"evidence": ...}` to `POST /api/engagements/<id>/milestones/<idx>/submit`
  and checks that the returned `evidence_hash` equals
  `Submission.evidence_hash`. The platform keeps only the hash; the harness
  keeps the Submission JSON and the artifacts.
- **Custody.** v1 specialists hold no payment or sub-hire tools (tool risk
  `external` is denied). `limits.max_usd` caps model spend on the VM; it is
  separate from the engagement budget and from mandates.
- **Release.** The buyer's `milestone.release` approval is still the
  acceptance step. The evidence says whether a licensed reviewer must sign
  off (`human_review`), but the platform does not yet hold a release for it.

### Domain tools and checks

Specialists add domain tools as plain functions listed in `TOOL_DEFS` and
domain checks listed in `CHECK_DEFS`; the kit finds both beside the package
and wraps them, so domain code never imports the loop.

- A tool is `fn(workspace, *, fetch=None, run=None, **args) -> dict | str`,
  declared as `{"name", "description", "input_schema", "function", "risk",
  "untrusted_output"}`. `risk` is one of `read | write | exec | network |
  external` (anything else is an error; `external` is denied in v1).
  `untrusted_output` defaults to true: the result reaches the model inside
  `<untrusted>` tags unless the def opts out.
- A function that names them also receives `resolve_path(p, *, write=False)`
  (the kit's path jail: inside the workspace, `inputs/` read-only,
  `.agentkit/` refused; `write=True` marks the file agent-authored, so it can
  never become a ledger source), `ledger` (e.g. `ledger.add_source(url, title,
  text)` for fetched data) and `events`. `workspace` itself is a plain `Path`:
  model-supplied paths must go through `resolve_path`, never through
  `workspace / path`.
- A check is `fn(workspace, params, *, run=None) -> {"passed", "details",
  "score"}`; the list form `{"name", "function", "kind"}` may fix its kind.
  Builtin checks have fixed kinds (`rubric_grader` rubric, `human_signoff`
  human, the rest automated) that no manifest or brief can change.
- `python -m agentkit validate SLUG` (and the start of every run) reports
  unknown tools or checks, a broken `checks.py` and missing rubric files.

### Workspace layout

```
inputs/          customer files, read-only to the agent
repo/            the customer repository (code engagements)
deliverables/    everything that is submitted
.agentkit/       internal: journal, checkpoints, sources, submissions
```

The kit's tools, and domain tools that use `resolve_path`, cannot write
under `inputs/` or touch `.agentkit/`; every path is checked lexically (no
drive, root, UNC, device or `..` escape) before it touches the filesystem, and
then resolved so symlink and junction escapes are caught too.

### Containment

The `PolicyGate` binds the kit's own tools. It cannot contain code that an
allowlisted program runs: `python -c`, a `conftest.py`, npm scripts and git
hooks can all write anywhere the VM user can (including `inputs/` and
`.agentkit/`), open sockets to any host and read outside the workspace. The
kit narrows the gap (programs are looked up on a scrubbed `PATH` and never run
from inside the workspace; each command gets an overall deadline and its whole
process tree is killed when it ends, so nothing outlives the step; the
environment is scrubbed of secrets), but a specialist whose manifest
allowlists any program must run inside an OS sandbox that enforces the same
boundaries: a container or VM where `inputs/` is mounted read-only, the kit's
state (`.agentkit/`) is outside the agent user's write access, and egress is
enforced by the host network. The ledger and submissions are tamper-evident
(snapshot hashes, the evidence hash) against single-file edits, not against a
process that can rewrite all of them.

### Egress

`egress.mode: none` denies all network; `allowlist` matches hosts against
`egress.allow`. Private, loopback, link-local and metadata addresses are
always denied - including numeric spellings (`2130706433`, `127.1`) and IPv6
forms that embed them - and a hostname is judged by what it resolves to: the
default transport checks every DNS answer and connects only to the address
it checked. Redirects are re-checked hop by hop; a cross-origin hop drops the
caller's headers (except User-Agent) and the body.

The brief can influence egress only when the manifest opts in with
`egress.intake_field` (an intake field holding a list of hostnames) and
`egress.intake_mode`: `narrow` (default; a host must match both the manifest
list and the intake list) or `extend` (adds exact hostnames only; `*` and
wildcards are refused).

### Submissions and the human gate

`run_milestone` runs `prepare -> Runner -> finalize -> checks` and always
builds a `Submission` with hashed artifacts (a provider outage ends the run
with outcome `model_error` and status `incomplete`). Status is
`ready_for_review` only when every automated check passed; rubric and human
checks may be pending (`passed=None`); a failed rubric or a result of unknown
kind gives `needs_revision`. `human_review.required` comes from the manifest
and cannot be turned off by the brief, the model or the CLI. Regulated
specialists produce work product for a licensed reviewer: they never file,
send or sign anything.

A brief may carry its own milestones (the signed scope). They are parsed as
strictly as the manifest's (safe id, workspace-relative deliverables, valid
kinds). A brief milestone that shares an id with a manifest milestone keeps
the manifest's acceptance checks and deliverables as a floor and can only add
to them; a brief-only milestone must include at least one automated check.

`run --resume` continues a stopped run with the same history (a user message
says it is resuming, so the history never ends on an assistant turn, and
delivers client answers that arrived since; a refused final turn is dropped
first, and every tool call in a checkpoint already has its result, so no
earlier turn is edited on replay); a milestone that was already
submitted returns its saved Submission unchanged, with the same evidence hash
- re-checking is the explicit `check` command. Only a `--milestone-idx` that
differs from the saved one changes it: the submission is re-bound to that
SOW index, with a new evidence hash.

The submission's `evidence_hash` is `"0x"` + sha256 (lowercase hex) of its
milestone evidence text, `platform_evidence(submission)`: compact canonical
JSON of at most 4000 characters that a harness posts to
`POST /api/engagements/<id>/milestones/<idx>/submit`, which returns the same
value. The text names the engagement, the SOW milestone index, the milestone
id, the status, whether human review is required, the artifacts (path,
sha256, bytes) and each check's outcome, and binds the rest of the Submission
(summary, check details, usage, questions, the models that produced the run)
through the digest of its integer-only record. When the text would exceed
the limit, the artifact list is replaced by `artifacts_count` and
`artifacts_sha256` (`"0x"` + sha256 of the list's canonical JSON), and if it
is still too long, the check list by `checks_count` and `checks_sha256` the
same way; a verifier must accept all three forms. Canonical JSON follows the
platform's rules
(`app/approvals/actions.py`: sorted keys, no whitespace, non-ASCII kept,
integers only), so cost is hashed as integer micro-USD and check scores as
integer basis points, and media types come from the kit's own table, so any
language on any host can recompute the hash. It is intended for the planned
escrow evidence field (#1).
Known secrets are redacted from the submission before hashing and from every
event; checkpoints are left byte-exact for resume and never leave the VM.

### Limits and cost

`limits` bound steps, tokens, USD and wall time; hitting one ends the run as
`budget_exceeded`. `max_tokens` meters uncached input, cache writes and
output (cache reads re-read the whole prefix every turn and are left to
`max_usd`). USD comes from the built-in Claude price table (which includes the
models server-side fallbacks route to) or `AGENTKIT_PRICING_FILE`; a model
with no known price adds no cost, so `max_usd` cannot bind on its tokens - the
run emits a `cost_unknown` event the first time that happens.

### Model defaults

`models.primary: anthropic:claude-opus-5`, grader `anthropic:claude-sonnet-5`.
A run uses the manifest's models, so in production it runs the model the
operator stamped (the platform manifest's `model` is `models.primary`).
Overrides are for development only and apply only with
`AGENTKIT_ALLOW_MODEL_OVERRIDE=1`: `--model` > `AGENTKIT_MODEL` > manifest,
fallbacks from `AGENTKIT_FALLBACK_MODELS` (comma list of refs), the grader
from `AGENTKIT_GRADER_MODEL` (a different model family is recommended).
Without the switch the CLI and `agentkit.llm.build_chain` ignore them (the
CLI says so on stderr).
`models.options` sets adapter options per provider (e.g.
`{anthropic: {effort: xhigh}}`). Claude's server-side fallbacks (a declined
turn re-served on another model) are off unless
`models.options.anthropic.server_fallbacks: true`. Like `models.fallbacks`,
that opt-in lives in `agent.yaml`, so a stamp that carries `spec_hash` covers
it; the stamped `model` names only the primary. Every switch, of the
fallback chain or server-side, is journaled as a `model_fallback` event, and
`Submission.models` lists the models that produced the run's turns, which
the evidence binds. Tests use `ScriptedAdapter` only: no network, no keys.

### Evals and the CLI

Golden cases live in `specialists/<package>/evals/cases/*.json`:
`{"milestone" (required), "name", "brief", "notes", "fixture" (a directory
under evals/fixtures/), "fixtures" (dest -> file under evals/fixtures/)}`;
`agentkit/evals.py` validates them. `python -m agentkit` exits 0 on success,
1 when `check`/`validate`/`validate-intake` find problems, 2 on usage or
configuration errors (nothing run) and 3 when `run` wrote a submission that is
not `ready_for_review`.

## Consequences

- One loop and one policy layer to review and harden for every specialist.
- `agent.yaml` is a private runtime spec; format changes bump
  `schema_version`, and any change to a runtime file changes `spec_hash`.
  Once the stamped manifest carries `spec_hash` (the proposed 0001
  amendment), such a change needs a re-stamp; until then the stamp binds
  only the fields decision 0001 lists (model, tools, MCP servers, skills,
  price and payout address).
- Provider SDKs are optional imports, so the web app never needs them.
- Deliberately out of scope for v1: sub-agents, MCP servers, persistent
  shells, and any tool with risk `external` (denied by the policy gate).
