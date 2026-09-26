# 0003 - Clean room and dependencies

Status: proposed · Date: 2026-09-26

## Context

The specialist kit (0002) borrows ideas from many open-source agent projects.
This repository is Apache-2.0. We want the ideas without inheriting code,
prompts or license obligations we did not choose, and without widening the
supply-chain attack surface of the agent VMs.

## Decision

### What is allowed

- Reading public docs, READMEs, papers, specs and architecture write-ups.
- Reading the source of permissively licensed projects to understand a design.
- Not allowed: copying, translating or closely paraphrasing third-party code,
  prompts, tool descriptions, skill text, test fixtures or data into this
  repository.

### Two-step clean room

1. A researcher (a person or a read-only research agent) writes a design note
   in their own words, citing repo and license, with no code beyond public API
   names.
2. The implementer builds from that note plus official specs, without the
   reference source open. For copyleft or source-available references the
   researcher and implementer must be different people or agent sessions.

Every reference is recorded in `docs/third_party/REFERENCES.md` with its
license and the idea taken.

### License tiers (against Apache-2.0)

| Tier | Licenses | Rule |
|---|---|---|
| GREEN | MIT, Apache-2.0, BSD-2/3, ISC, PSF, Zlib, CC0, Unlicense | usable as dependencies; keep notices; propagate Apache NOTICE files when redistributing |
| YELLOW | MPL-2.0, LGPL, CC-BY-4.0 docs, source-available or commercial terms (ELv2, BSL, SSPL, vendor commercial terms) | never copy code or text; dependency use needs an explicit decision record |
| RED | GPL-2.0/3.0, AGPL-3.0, no license | ideas from public docs only; no code reuse, no linking into the kit or anything shipped to customers |

### Dependency vetting

A new dependency must be justified in the PR description and checked for:
license tier, maintenance (recent releases, more than one maintainer),
transitive weight, install-time code execution, and known supply-chain
incidents. Prefer the standard library (the kit reads `.docx` with
`zipfile` + `xml`, HTML with `html.parser`). Provider SDKs are imported
lazily and live in `requirements-agents.txt` so the web app never needs them.

Approved for the kit so far:

- `PyYAML>=6.0.2` (MIT) - manifests and rubrics, `safe_load` only.
- `anthropic` (MIT) and `openai` (Apache-2.0), the official vendor SDKs -
  model adapters only. `openai` is Apache-2.0: when it is redistributed (for
  example baked into a VM image), its license and any NOTICE file go with it.

Explicitly avoided as in-VM dependencies: LiteLLM (compromised PyPI releases
in 2026 and a heavy tree) and frameworks that pull it in transitively.

## Consequences

- Slightly more code of our own, all of it reviewable under one license.
- Reviewers check new references and dependencies against this record.
