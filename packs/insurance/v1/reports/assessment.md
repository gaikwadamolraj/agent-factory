This file is a report template. Every `{{PLACEHOLDER}}` is replaced once, in a single pass, with text the system
has already computed; the template carries no loops, conditions or logic. A placeholder with nothing to fill it is
rendered as "Not stated in the documents". The rendered report is a proposal for a claims handler: it records a
position and its evidence, and it is not a decision, an acceptance, a declinature or an offer of settlement.

# Claim assessment - {{CLAIM_REFERENCE}}

| | |
|---|---|
| Claim reference | {{CLAIM_REFERENCE}} |
| Policy number | {{POLICY_NUMBER}} |
| Product | {{PRODUCT_TYPE}} |
| Insured | {{INSURED_NAME}} |
| Insurer | {{INSURER_NAME}} |
| Policy period | {{POLICY_PERIOD}} |
| Date of loss | {{LOSS_DATE}} |
| Date reported | {{REPORTED_DATE}} |
| Cause of loss as stated | {{CAUSE_OF_LOSS}} |
| Amount claimed | {{CLAIMED_AMOUNT}} |
| Assessed on | {{ASSESSMENT_DATE}} |
| Prepared by | {{PREPARED_BY}} |
| Request id | {{REQUEST_ID}} |

## Proposed position

Positions used in this report are exactly: covered, conditional, excluded, insufficient_evidence,
not_applicable. Each section below lists the claimed items that carry that position.

{{OVERALL_POSITION}}

{{POSITION_SUMMARY}}

## Covered

{{COVERED_ITEMS}}

## Conditional

{{CONDITIONAL_ITEMS}}

Conditions still to be satisfied: {{CONDITIONS_TO_SATISFY}}

## Excluded

{{EXCLUDED_ITEMS}}

Policy wording relied on: {{EXCLUSION_QUOTES}}

## Insufficient evidence

{{INSUFFICIENT_EVIDENCE_ITEMS}}

Information outstanding: {{OUTSTANDING_INFORMATION}}

## Not applicable

{{NOT_APPLICABLE_ITEMS}}

## Policy terms relied on

| Reference | Kind | Wording |
|---|---|---|
| {{CLAUSE_REFERENCES}} | {{CLAUSE_KINDS}} | {{CLAUSE_QUOTES}} |

Limits and sub-limits: {{POLICY_LIMITS}}

Excess or deductible: {{DEDUCTIBLE}}

Endorsements applied: {{ENDORSEMENTS_APPLIED}}

## Evidence used

{{EVIDENCE_LIST}}

Documents still referenced but not supplied: {{DOCUMENTS_NOT_SUPPLIED}}

## Data quality and integrity

Gaps in the documents: {{DATA_GAPS}}

Conflicting statements: {{CONFLICTING_STATEMENTS}}

Manipulation check: {{MANIPULATION_NOTE}}

## Next steps for the handler

{{RECOMMENDED_NEXT_STEPS}}

## Human approval

No position in this report has been communicated to the claimant and no payment, declinature or repudiation has
been made. A claims handler must review the evidence and decide.

| | |
|---|---|
| Reviewer | {{HUMAN_REVIEWER}} |
| Approval status | {{APPROVAL_STATUS}} |
| Approved on | {{APPROVAL_DATE}} |
| Handler notes | {{HANDLER_NOTES}} |
