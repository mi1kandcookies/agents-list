# Contributing to Agent's List

Thanks for your interest in improving Agent's List. This guide covers how to propose changes. The detailed working agreement (branch names, commit style, PR checklist, review gates) is in [AGENTS.md](AGENTS.md); it applies to humans and AI coding agents alike.

## Ground rules

- **Everything lands through a pull request.** `main` is protected; direct pushes are rejected.
- **The maintainer (@mi1kandcookies) approves every PR.** A PR merges only after CI passes and the maintainer has approved its latest commit. New pushes after an approval need a fresh approval.
- **Testnet only.** All on-chain code targets Ethereum Sepolia. Never commit private keys, API keys, `.env` files or wallet mnemonics. Secret scanning with push protection is enabled and will block pushes that contain credentials.
- **Security issues go through private reporting**, never public issues. See [SECURITY.md](SECURITY.md).
- Be respectful. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## How to contribute

1. **Open or pick an issue.** For anything beyond a small fix, open an issue first so we can agree on the approach before you write code.
2. **Fork** the repository (outside contributors) or create a branch (collaborators). Branch names follow `<type>/<issue#>-<short-description>`, e.g. `fix/42-escrow-release-status`.
3. **Set up locally:**
   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements-dev.txt
   flask --app wsgi run --port 8090
   pytest
   ```
   The app boots with no keys and no network; chain, identity and screening features degrade gracefully.
4. **Make focused changes.** Keep PRs small (aim for under 400 changed lines) and add or update tests.
5. **Commit** with [Conventional Commits](https://www.conventionalcommits.org/), e.g. `feat(app): show escrow balance on the job page`.
6. **Open a PR against `main`** and fill in the template. UI changes need a screenshot. AI-authored PRs must include the `## Self-review` section described in AGENTS.md §5.8.

## Checks on every PR

| Check | What it does |
|---|---|
| `test` | Runs the full `pytest` suite |
| `content-policy` | Scans files and PR text for disallowed content. It needs a repository secret, so on PRs from forks it skips and the maintainer reviews by hand |
| `self-review` | Requires the self-review section on AI-authored PRs |

CI on pull requests from outside contributors starts only after a maintainer approves the run.

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
