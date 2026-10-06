# Codex project rules

## Sources of truth

- `docs/roadmap.md` is the approved phase plan; Phase 12 is the fixed finish line.
- `docs/architecture/system-overview.md` and `docs/architecture/integration-boundaries.md` define the architecture and future ownership boundaries.
- If a request conflicts with those documents, surface the conflict before expanding scope.

## Engineering rules

- Keep this a modular monolith.
- Prefer YAGNI, small explicit code, and deterministic application control of side effects.
- Routes stay thin: validate/request-coordinate at the edge and keep behavior in the smallest relevant module.
- Database schema changes go through Alembic migrations; do not edit production databases manually.
- Future provider access belongs behind explicit integration boundaries only when that integration has a real implementation.
- Normal CI must not call live external providers or SaaS services.
- Add meaningful behavior tests; never weaken or delete tests to make code pass.

> Do not create an abstract adapter, Protocol, base class, registry, repository framework or generic provider layer merely because a future phase may need one. Introduce the smallest abstraction only when the first real implementation requires it and the actual interface is known.

## Scope and Git discipline

- Work only on the requested phase branch and keep commits reviewable and scoped.
- Do not use destructive Git commands such as `reset --hard`, force-push, or history rewriting.
- Codex cannot merge branches, create/merge PRs, or mark milestones `COMPLETE`.
- Phase 0 must end as `REVIEW` pending independent human review.

## Required closeout report

Close implementation work with exactly these sections:

1. Status
2. Git
3. Files changed
4. Implementation summary
5. Architectural decisions
6. Tests executed
7. Quality/tooling results
8. Manual verification
9. Deviations
10. Known limitations
11. Security and cost
12. Scope audit

Include exact commands/results, starting and ending SHAs, branch/working-tree state, deviations, unresolved limitations, security findings, and confirmation that no future-phase functionality was added.

