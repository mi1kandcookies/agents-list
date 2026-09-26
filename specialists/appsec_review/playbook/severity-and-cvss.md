# Severity and CVSS

Score every finding with a CVSS v3.1 base vector and let `cvss_base_score`
compute the number - never hand-write the score. A vector looks like:

    CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H

Metrics: AV (Attack Vector N/A/L/P), AC (Complexity L/H), PR (Privileges
Required N/L/H), UI (User Interaction N/R), S (Scope U/C), and C/I/A (Impact
H/L/N).

Severity bands (used as the SARIF level and `security-severity`):

| Base score | Severity | SARIF level |
|---|---|---|
| 9.0 - 10.0 | Critical | error |
| 7.0 - 8.9  | High     | error |
| 4.0 - 6.9  | Medium   | warning |
| 0.1 - 3.9  | Low      | note |
| 0.0        | None     | none |

Guidance:

- An unauthenticated, network-reachable injection that reads or writes data is
  usually High/Critical (`AV:N/PR:N` with `C:H`/`I:H`).
- Requiring authentication drops PR to L/H and lowers the score - reflect the
  real precondition, do not inflate.
- Hard-coded secrets: severity depends on what the secret unlocks. A live cloud
  key is Critical; a secret for a disposable dev service is lower.
- Do not stack unrelated impacts into one finding to raise its score. One
  weakness, one finding, one honest vector.

Put the vector string on the finding (`cvss`). `build_sarif` computes the score,
band and SARIF level from it, and the `cvss_consistent` check re-derives them,
so a hand-edited score or level fails. The vector must start with `CVSS:3.1/`
and name each base metric exactly once.
