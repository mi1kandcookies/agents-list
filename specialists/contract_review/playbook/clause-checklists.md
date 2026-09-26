# Clause families and what to look for

Family ids below are the v1 defaults; a client playbook may add more. The
"look for" lists are prompts for careful reading, not conclusions. The
client's playbook decides what is acceptable.

| id | Look for |
|---|---|
| `liability_cap_amount` | Is there a cap at all? Is it mutual? A fixed amount or a multiple of fees? Does a low cap swallow the value of the deal? |
| `liability_cap_base` | Fees *paid* versus *paid or payable*; the look-back window; per-order versus whole-agreement base; what happens in the first months when little has been paid. |
| `liability_indirect_damages` | One-way versus mutual exclusion; whether lost profits, lost data or regulatory fines are swept into "indirect"; whether the exclusion also covers the carve-outs. |
| `liability_carve_outs` | Indemnities, confidentiality breaches, data breaches, gross negligence, wilful misconduct, fraud, unpaid fees. A clause that applies the cap "to all claims, including" indemnities is the classic trap. Look for super-caps. |
| `indemnification` | Who indemnifies whom and for what. "Any breach" or first-party losses turn an indemnity into uncapped damages. IP infringement coverage, defence control, exclusions, and whether indemnities sit under the cap. |
| `intellectual_property` | Ownership of deliverables and customer data; licence grants back to the provider; feedback licences; any right to use customer data to train or improve products; residuals clauses. |
| `data_protection` | A DPA (or its absence), security commitments, breach-notice window ("commercially reasonable time" is not a window), sub-processors, data location, deletion and return at end of term. |
| `confidentiality` | Mutuality, standard of care, permitted disclosures, survival period, trade secrets, return or destruction, whether "Confidential Information" is actually defined. |
| `warranties` | Performance warranty or a full "as is" disclaimer; warranty period; exclusive remedies; disclaimers that conflict with the SLA. |
| `term_termination` | Initial term, auto-renewal, non-renewal notice window, renewal pricing, termination for convenience and for cause, cure periods, effects of termination and transition help. |
| `assignment` | One-sided assignment rights, change of control, assignment to competitors, termination rights on assignment. |
| `governing_law` | Law and forum, arbitration, jury waiver, whether the forum is neutral or the counterparty's home court. |
| `insurance` | Types and minimum limits, additional insured status, certificates. Absence matters most when the provider hosts client data. |
| `payment` | Payment terms, late interest, unilateral price changes, taxes, disputes and suspension for non-payment, renewal uplifts. |
| `non_solicitation` | One-way restrictions, duration, general-advertising exception, enforceability concerns (flag for the attorney). |

## Cross-cutting checks

- **Definitions** widen or narrow everything. Check capitalised terms used
  but never defined, and definitions nobody uses.
- **Cross-references** to missing sections (run `check_references`).
- **Order of precedence** between the agreement, order forms and policies
  incorporated by URL; online terms the provider can change unilaterally.
- **Missing provisions** the playbook expects count as issues, quoted with
  the nearest related text and marked in coverage.
