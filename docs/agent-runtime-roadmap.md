# Agent Runtime Core Roadmap

## Goal

Build a runtime layer between the execution safety boundary and business workflows.

## Architecture

```
Workspace UI
    |
Adapter Layer
    |
Agent Runtime Core
    |
Execution Gateway
    |
Model / Tool Boundary
    |
Business Workflow
```

## Principles

- UI and business logic stay decoupled.
- Agent runs are represented as explicit runtime objects.
- All side effects continue through the execution gateway.
- Product-specific workflows remain outside the core runtime.

## Next Components

- Agent definition registry
- Runtime session manager
- Context management
- Tool registry adapter
- Workspace integration adapter
