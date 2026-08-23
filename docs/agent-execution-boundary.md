# Agent Execution Boundary

## Purpose

All future agent side effects must pass through a single authorization boundary.

## Flow

```
Job
 |
v
JobAuthorityGuard
 |
v
ExecutionGateway
 |
+-----------+
|           |
v           v
Model      Tool
```

## Rules

- Terminal jobs cannot continue execution.
- Human intervention states cannot continue autonomous execution.
- Expired leases cannot continue execution.
- Binding mismatch fails closed.

## Extension point

Future Agent runtimes should call the gateway instead of directly invoking model or tool side effects.
