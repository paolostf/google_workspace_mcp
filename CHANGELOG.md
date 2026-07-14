# Changelog

## [Unreleased]

### Added

- Add compact Gmail message indexing, metadata batching, thread-level label batching, and draft listing/upsert primitives for the local Inbox Operator. Cause: exhaustive unread processing cannot safely rely on verbose search output, message-only archiving, or create-only drafts. Impact: the local runtime can paginate, reconcile, and resume without duplicate drafts or partial thread state. Evidence: local full suite at 1,268 passed and 2 skipped on 2026-07-14.
- Add isolated encrypted CLI OAuth profiles and optional scope requests. Cause: three Gmail accounts share one remote server but require separate durable sessions. Impact: each account can keep its own local OAuth state; the server permission configuration remains the enforceable scope boundary.

### Changed

- Extend Gmail tool tiers and operator documentation for the new read, thread-label, and draft-reconciliation primitives. Cause: the restricted Inbox Operator service needs an explicit Gmail-only complete surface. Impact: deployment can expose the required tools without enabling Gmail send or unrelated Workspace products.

### Baseline

- Changelog initialized on 2026-07-14 at commit `787ca6b2a0cacf8bb5bfa903d572f1f48feac90c`. Earlier verified history remains in Git and upstream release records.
