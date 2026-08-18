# Local operations runbook

## Start

Prerequisites are Python 3.12+, Node 20.19+ (verified pin: 24.18.0), installed
Python dependencies, and `frontend/node_modules`. Copy `.env.example` to `.env`
and fill only the integrations you actually have. Never commit `.env`, cookies,
browser profiles, API keys, or phone evidence.

From the repository root:

```powershell
.\scripts\run-local.ps1
```

The script starts hidden local-only processes on `127.0.0.1`, writes their PIDs
to `<runtime>\run\local-processes.json`, and writes stdout/stderr under
`<runtime>\logs`. The dashboard is `http://127.0.0.1:5173`; factual prerequisite
status is `http://127.0.0.1:8000/api/v1/health`.

To stop a run, inspect the PID file, confirm the two PIDs belong to this
workbench, then call `Stop-Process -Id <pid>`. Do not bulk-kill Python or Node.

## Verify

```powershell
.\scripts\verify.ps1
```

This runs the complete backend suite, Python compilation, Vitest, production
frontend build, Playwright's controlled fresh-SQLite flow, dependency audit,
tracked-file secret scan, and destructive-source boundary scan. Controlled
adapters are software verification, not live Qianfan, Android, or Bailian proof.

## Evidence and recovery

- SQLite: `<runtime>\workbench.sqlite3`
- job evidence: `<runtime>\evidence`
- content artifacts: `<runtime>\content`
- cleanup quarantine: `<runtime>\.artifact-quarantine`
- process logs/PIDs: `<runtime>\logs` and `<runtime>\run`

Never manually delete an artifact referenced by SQLite. Product/content cleanup
is queued, quarantined on the same volume, and delayed. Job-result temporary or
final bytes are deliberately retained when cancellation, rollback, or commit
acknowledgement makes ownership uncertain; inspect the job state and the hidden
`.tmp`/final result under its evidence directory before deciding any manual
action.

Recovery guide:

| Fact | Expected state | Operator action |
|---|---|---|
| Cookie/login prompt | `needs_human`, login evidence | Re-authenticate in the configured persistent profile, then enqueue a new collection. |
| Captcha | `needs_human`, screenshot/UI evidence | Complete it manually; the system does not bypass it. |
| Layout/selector change | `needs_human`, selector profile recorded | Stop collection and update/verify a new selector profile before retry. |
| Android disconnect | `needs_human` or failed with device evidence | Reconnect the one approved device, verify foreground app/login, enqueue a new run. |
| Model network/rate limit | bounded retries, then failed | Check health/key/network; retry as a new auditable analysis. |
| Expired process lease | `needs_human` with `worker_interrupted` or `worker_restart_required` | Inspect evidence and explicitly restart; no automatic success/resume is inferred. |
| Cleanup `needs_human` | bytes retained in quarantine | Inspect read-only cleanup API and DB/file identity; do not manually reuse its path. |

## Intentional analysis rerun policy

`analyses.input_digest` is intentionally not unique. The same evidence and
prompt may be rerun to obtain a new, separately audited provider response;
opportunities remain children of that analysis and are not merged silently.
The UI single-flight fence prevents accidental rapid double-submit, while an
operator-requested identical rerun remains valid. This is an explicit V1
decision, not a missing database constraint.
