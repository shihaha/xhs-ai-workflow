# Phase A Cross-account Demand Validation Design

Date: 2026-08-20
Status: approved for implementation

## Decision

Reuse the existing grounded analysis and sealed evidence system. Do not create a parallel research subsystem and do not extend product/content execution.

Single-account reports are observation signals only. Cross-account analyses require at least two distinct accounts, and each supporting account must contribute one complete shop result plus trusted account-note evidence. The model proposes a cluster and citations; the service validates ownership and computes the evidence level.

## Opportunity lifecycle

- Two supporting accounts: `warming_candidate`.
- Three or more supporting accounts: `validated_candidate`.
- A valid cluster is inserted as `pending_review`.
- Human approval changes only review state to `approved`.
- Human rejection requires a reason and changes review state to `rejected`.
- Review is one-way. Evidence, account count and evidence level are immutable facts.
- Only `approved` opportunities may enter the product boundary; Phase A exposes no product action.

## Candidate pool truthfulness

Qianfan responses may omit ranges required by the tutorial scoring formula. Such accounts remain visible with `score_status=insufficient_metrics`. They are ordered by factual ranking recurrence, best rank and stable account identity; the system never labels a synthetic zero as a real score.

## Phase A stopping point

Phase A ends after one isolated real validation using at least two independent accounts, documented tests/UAT, Git commits and verified push to `feature/system-v1`. It does not start Phase B.
