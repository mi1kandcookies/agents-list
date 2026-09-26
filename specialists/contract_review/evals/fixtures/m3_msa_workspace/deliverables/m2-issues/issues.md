# Issue list - inputs/northwind_saas_msa.txt

Draft work product prepared for review by a licensed attorney. Not legal advice. Nothing in this deliverable has been sent to any counterparty, accepted, rejected or signed.

Playbook: `deliverables/m1-playbook/playbook.yaml`. Contract sha256: `88e31ff7ac38f512269589599bb27e0abc0375bffc217b9ea63b4f160a984998`.

| ID | Severity | Family | Location | Deviation | Recommendation |
|---|---|---|---|---|---|
| I2 | critical (escalate) | liability_carve_outs | §9.3 p37 | The cap swallows indemnity and confidentiality claims. | Carve indemnity and confidentiality breaches out of the cap. |
| I3 | critical (escalate) | indemnification | §8.1 p32 | One-way customer indemnity for any breach. | Replace with a provider IP indemnity; narrow the customer indemnity. |
| I1 | high | liability_cap_amount | §9.2 p36 | Cap is three months of fees; the playbook wants twelve. | Raise the cap to twelve months of fees paid or payable. |
| I4 | high (escalate) | intellectual_property | §5.2 p22 | Perpetual licence to train on Customer Data. [review] | Limit the licence to feedback used to improve the Services. |
| I5 | high | data_protection | §6.2 p26 | No fixed breach-notice window and no DPA. | Notice within 72 hours and a DPA as an exhibit. |
| I10 | medium | liability_cap_base | §9.2 p36 | Cap base counts fees paid only. | Use fees paid or payable. |
| I6 | medium | warranties | §7.1 p29 | Full as-is disclaimer. | Add a performance warranty. |
| I7 | medium | term_termination | §3.2 p15 | 90-day non-renewal notice. | Shorten the window to 30 days. |
| I8 | medium | payment | §2.2 p11 | Unilateral mid-term price increases. | Increases only at renewal, capped at 5%. |
| I9 | medium | assignment | §10.1 p40 | One-sided assignment right. | Make assignment mutual. |

## Details

- **I2** (§9.3 p37): "The limitations in Section 9.2 apply to all claims, including claims under Section 8 and breaches of Section 4."
  - Fallback: Data-protection breaches under a super-cap of 3x the general cap.
- **I3** (§8.1 p32): "hold harmless Provider from any claim arising out of any breach of this Agreement"
  - Fallback: Mutual third-party-claim indemnities limited to IP infringement.
- **I1** (§9.2 p36): "shall not exceed the fees paid by Customer in the three (3) months preceding the claim"
  - Fallback: Six months with a USD 250,000 floor.
- **I4** (§5.2 p22): "any Customer Data for any purpose, including to train"
  - Fallback: Aggregated, de-identified usage data only, never for training.
- **I5** (§6.2 p26): "notify Customer of a security incident affecting Customer Data within a commercially reasonable time"
  - Fallback: Notice within five business days.
- **I10** (§9.2 p36): "the fees paid by Customer in the three (3) months"
  - Fallback: Fees paid in the 12 months before the claim.
- **I6** (§7.1 p29): "PROVIDER DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED"
  - Fallback: A 90-day warranty with re-performance as the remedy.
- **I7** (§3.2 p15): "at least ninety (90) days before the end of the then-current term"
  - Fallback: 45 days.
- **I8** (§2.2 p11): "Provider may increase the fees at any time"
  - Fallback: Capped at 7%.
- **I9** (§10.1 p40): "Provider may assign this Agreement without consent"
  - Fallback: Provider may assign only to a successor that is not a Harborlight competitor.

## Playbook coverage

| Family | Status | Issues, quote or note |
|---|---|---|
| liability_cap_amount | deviation | I1 |
| liability_cap_base | deviation | I10 |
| liability_indirect_damages | compliant | "In no event shall either party be liable for any indirect, incidental, special or consequential damages" |
| liability_carve_outs | deviation | I2 |
| indemnification | deviation | I3 |
| intellectual_property | deviation | I4 |
| data_protection | deviation | I5 |
| confidentiality | compliant | "shall protect the other party's Confidential Information using at least reasonable care" |
| warranties | deviation | I6 |
| term_termination | deviation | I7 |
| assignment | deviation | I9 |
| governing_law | compliant | "governed by the laws of the State of Delaware" |
| insurance | absent | The contract has no clause on this. |
| payment | deviation | I8 |
| non_solicitation | absent | The contract has no clause on this. |
