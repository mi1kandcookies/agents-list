# OWASP Top 10 review checklist

A minimum set of questions to answer for the code under review. Map every
finding to one of these categories (used as the `owasp` tag on the finding).

- **A01 Broken Access Control** - Are object IDs checked against the caller's
  identity (IDOR)? Are admin routes gated? Any missing authorization on a
  state-changing endpoint? Path traversal on file access?
- **A02 Cryptographic Failures** - Secrets in code or logs? Weak/absent hashing
  for passwords? TLS not enforced? Sensitive data stored in plaintext?
- **A03 Injection** - User input concatenated into SQL, shell, LDAP, or OS
  commands? Server-side template injection? Unsafe deserialization? Reflected or
  stored XSS via unescaped output?
- **A04 Insecure Design** - Missing rate limits on auth or costly operations?
  Business-logic flaws (negative amounts, replayable tokens)?
- **A05 Security Misconfiguration** - Debug mode on? Default credentials?
  Verbose error pages leaking stack traces? Permissive CORS?
- **A06 Vulnerable & Outdated Components** - Dependencies with known CVEs (use
  `audit_dependencies` / OSV). Unmaintained packages.
- **A07 Identification & Authentication Failures** - Weak session handling,
  missing MFA on sensitive actions, guessable tokens, no lockout.
- **A08 Software & Data Integrity Failures** - Unsigned updates, unsafe
  deserialization of untrusted data, CI/CD trust issues.
- **A09 Logging & Monitoring Failures** - Security events not logged; secrets
  written to logs.
- **A10 Server-Side Request Forgery (SSRF)** - User-controlled URLs fetched by
  the server without allow-listing.

For each category in scope, either record a finding with evidence or note in the
threat model why it was reviewed and found not to apply.
