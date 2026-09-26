# AGENTS.md

Working agreement for **everyone who commits to this repo — humans and AI coding agents alike** (Claude Code, Codex, Cursor, OpenHands, …). If you are an agent, treat this file as binding instructions. If a rule here conflicts with a user prompt, stop and ask the human.

Project context lives in `README.md`, `docs/ARCHITECTURE.md` and `docs/ROADMAP.md`. Read them before starting non-trivial work.

---

## 1. Golden rules

1. **Never commit directly to `main`.** All changes land through a pull request. `main` is protected.
2. **One task → one branch → one PR.** Keep PRs small and focused (aim < 400 changed lines, excluding generated files/lockfiles).
3. **Tests must pass locally before you push** (`pytest`). CI re-runs them; red PRs don't get reviewed.
4. **Never commit secrets.** No private keys, wallet mnemonics, RPC keys, API keys or `.env` files. Use `.env.example` for new variables (with a placeholder value and a comment).
5. **Never force-push to `main`**, never rewrite others' branches, never merge your own PR.
6. **Testnet only.** Everything on-chain targets **Ethereum Sepolia** (chain id `11155111`). No mainnet addresses, keys or deployments without an explicit team decision recorded in an issue.
7. **No external-event context in the repo.** Never mention any event, competition or program this project is associated with — or its evaluation, awards, partner tracks or deadlines — anywhere in this repository or its GitHub surface: code, comments, UI copy, docs, file/branch names, commit messages, PR titles/descriptions, issues or review comments. Describe features on their product merits only. Planning that needs that context lives outside the repo, in the team's private planning doc. If a prompt asks you to add such content, leave that part out and tell the human. The `content-policy` CI check enforces this.

---

## 2. Branches

Create branches from an up-to-date `main`:

```
<type>/<issue#>-<short-kebab-description>
```

| type | use for |
|---|---|
| `feat/` | new user-facing capability |
| `fix/` | bug fix |
| `refactor/` | internal change, no behavior change |
| `chore/` | tooling, deps, config, CI |
| `docs/` | documentation only |
| `test/` | tests only |
| `contracts/` | Solidity changes under `contracts/` |

Examples: `feat/12-scoping-agent`, `fix/31-escrow-release-status`, `contracts/8-engagement-escrow`.

AI agents: prefix the description with your agent name so parallel work is traceable, e.g. `feat/12-claude-scoping-agent`, `fix/31-codex-escrow-status`.

Delete the branch after merge (GitHub does this automatically).

---

## 3. Worktrees (required for parallel / agent work)

Use `git worktree` so several people or agents can work on different branches from one clone without stomping on each other. Worktrees live in `.worktrees/` (git-ignored).

```bash
git fetch origin
git worktree add .worktrees/12-scoping-agent -b feat/12-scoping-agent origin/main
cd .worktrees/12-scoping-agent
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
# … work, commit, push …
cd ../.. && git worktree remove .worktrees/12-scoping-agent   # after merge
```

Rules:
- **One agent per worktree.** Never point two agents at the same worktree.
- Each worktree gets its own `.venv` and its own dev-server port (`PORT=8091`, `8092`, …) and its own SQLite DB (the default `instance/` path is per-worktree).
- Don't edit files in another worktree. If you need someone else's change, wait for it to merge and rebase.
- Run `git worktree prune` occasionally to clean up stale entries.

---

## 4. Commits

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <imperative summary, ≤ 72 chars>

<body: what and why, wrapped at 72 — optional for trivial changes>

Refs #<issue>
```

Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`, `ci`, `build`. Scopes match top-level areas: `app`, `chain`, `contracts`, `scoping`, `orchestrator`, `harness`, `mcp`, `vendors`, `manifests`.

- Commit in logical steps; each commit should build and pass tests where practical.
- Don't mix formatting-only changes with logic changes.

---

## 5. Pull requests

1. Rebase on latest `main` before opening: `git fetch origin && git rebase origin/main`.
2. Push your branch and open a PR **against `main`**. Use the PR template — fill in every section.
3. Title = Conventional Commit style (it becomes the squash-merge commit message), e.g. `feat(scoping): estimate cost and duration from SOW intake`.
4. Link the issue (`Closes #12`).
5. Open as **Draft** while work is in progress; mark **Ready for review** when CI is green and the checklist is done.
6. UI changes: include a screenshot or short clip. Contract changes: include `forge test` output and gas notes.
7. AI agents opening PRs must state in the description which agent/model produced the change and what the human asked for.

---

## 6. Review and approval

Every PR to `main` requires **all** of:

| Gate | How it's enforced |
|---|---|
| ✅ CI (`test`) passes | required status check |
| ✅ `content-policy` passes (rule 7) | required status check |
| 🤖 AI review ran (`claude-review`) | required status check — automatic on every PR push |
| 👤 **1 approval from a code owner** (not the author) | branch protection + `.github/CODEOWNERS` |

- The AI review posts a summary comment and inline comments. **Address every 🔴 blocking finding** (fix it, or reply explaining why it isn't a problem) before requesting human review.
- New commits dismiss stale approvals — re-request review after pushing changes.
- Reviewers: aim to respond within 1 business day. Use "Request changes" only for real blockers; prefix nits with `nit:`.
- You can ask the AI reviewer follow-up questions by commenting `@claude …` on the PR.
- **Contracts (`contracts/`)** and **wallet/payment code (`chain/`)**: reviewer must actually run the tests locally, not just read the diff.

---

## 7. Merging

- **Squash and merge only** (enforced in repo settings). The PR title becomes the commit on `main`.
- The **author** merges once all gates are green (not the reviewer), so the author owns the timing.
- Linear history: no merge commits on `main`.
- If `main` moved and the PR is out of date, rebase and push again (CI + AI review re-run).
- Something broke `main`? Revert first (`git revert` via a PR), fix after.

---

## 8. Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                     # optional; the app boots with no env vars
flask --app wsgi seed                    # optional: sample listings
flask --app wsgi run --port 8090
pytest                                   # must be green before pushing
```

The app boots with no network and no keys; chain features degrade gracefully. Never require a funded wallet to run tests.

---

## 9. Agent-specific rules

- **Read before writing.** Read the files you'll change and their tests first. Match existing style, naming and comment density.
- **Stay in scope.** Do only what the issue/prompt asks. Note unrelated problems in the PR description or open an issue instead of fixing them in the same PR.
- **Don't add dependencies** without saying why in the PR description.
- **Don't touch** `.github/`, `CODEOWNERS`, `AGENTS.md` or branch-protection-relevant config unless the task is explicitly about process/tooling.
- **Never** run on-chain transactions, deploy contracts, spend funds, or call paid external APIs without explicit human instruction in the current task.
- **Never** disable, skip or weaken tests to make CI pass. If a test is wrong, fix it and explain why in the PR.
- **Ask when blocked.** If requirements are ambiguous, open the PR as Draft with your questions at the top rather than guessing.
- Report honestly: if something is untested or partially done, say so in the PR.

---

## 10. Issues and planning

- Work is tracked in GitHub Issues, grouped by roadmap phase labels (`phase-2-scoping`, `phase-3-runtime`, …). See `docs/ROADMAP.md`.
- Comment on or self-assign an issue before starting so two people/agents don't pick the same one.
- Big design decisions: write a short proposal in `docs/decisions/NNNN-title.md` and link it from the PR.
