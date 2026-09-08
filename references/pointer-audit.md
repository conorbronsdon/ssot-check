# Pointer audit

Use for missing SSOT routes, orphaned handoffs, duplicated setup guides, and
confusion about which repo owns a connection. This workflow complements the
fact-checking CLI; it is not an automated link crawler or credential scanner.

## Establish the reader's starting point

Start with the user's actual task and the entry files the runtime or reader
loads: for example `AGENTS.md`, a shared contract, `ROUTING.md`, or `README.md`.
Follow their relevant links. Then search the named repos' tracked filenames for
the missing topic, including setup guides, configuration indexes and handoffs.
Use `rg --files` / `rg -l` or `git ls-files` / `git grep -l` so discovery reports
paths rather than dumping configuration contents.

Record the repo root, branch, HEAD and dirty state. An old or non-default
checkout may lack a guide already on the default branch. Inspect the known
remote/default tree when available, stating whether it was refreshed. Do not
switch, reset, stash or merge a dirty checkout to make an audit look current.
Use `git worktree list` when a task checkout differs from the configured one.
Resolve cross-repo paths against the declared owner, not an assumed sibling of
a deeply nested worktree. If that repo is unavailable, say so.

## Separate ownership from evidence

Trace the smallest useful route:

`entry point -> owner guide -> implementation/config location -> verification`

An existing sync or repo map may describe how shared files propagate; it does
not necessarily own service credentials or live connection state. Preserve that
boundary rather than adding a competing catch-all map.

For a connection, distinguish these claims:

| Claim | Evidence needed |
|---|---|
| Setup is documented | Maintained guide, reachable from a relevant entry point |
| Configuration exists | Named configuration and key are present in the configured checkout/runtime |
| Current process can use it | Correct environment binding or tool registration is loaded |
| Read access works | Successful bounded read through the intended service and scope |
| Background operation works | Configured scheduler plus observed runs/health evidence |

Document paths, key names and binding relationships, never credential values.
An ignored config missing from a worktree, a browser login, or an empty channel
list from another bridge does not settle the other claims. Do not hunt through
arbitrary secret stores or ask for a token before checking the documented owner.
When a live check is relevant and authorized, reuse its existing configuration
in-process, report only necessary results, and keep the probe read-only.

## Repair the route

Prefer an existing durable guide as the owner. Add the missing pointer at the
reader's real entry point and from the workflow that consumes the connection.
Keep setup details in the owner guide; other repos should link to them. Use
portable repo-relative links within a repo and explicit repository links across
repos. Document local checkout resolution separately where it matters.

A repair already on the default branch is not a new change to recreate on an
old branch. Before adding a local redirect, verify its target exists in that
same checkout. If it exists only on the default branch, link to that explicit
repository/ref or leave the old checkout unchanged until it is updated. Do not
create a broken local link while reporting the remote route as fixed.

Promote useful handoff facts into the maintained guide. Replace active duplicate
instructions with a short redirect; preserve dated evidence in history or an
explicit archive. A recorded assumption must remain an assumption until tested.
Do not rewrite historical records to imply today's verification happened then.

Check implementation claims against source where available: matching rules,
environment variable names, and actual storage paths often outlive stale prose.
A documentation repair must not silently widen service access or copy secrets.
Follow the user's authorization: propose fixes for an audit-only request, and
apply scoped edits for an explicit fix request. Missing access to a repo does
not grant authority to create a replacement owner elsewhere.

## Verify and report

Walk the route again from the original entry point without relying on the
conversation. Resolve changed local links and cross-repo file targets against
the selected repo snapshots; distinguish file existence from a live URL check.
Use the repo's link checker when it covers the changed links. It may skip
external links, anchors, untracked files, or reference-style links; inspect those
separately. Confirm old entry paths redirect and competing active instructions
are gone. This is a bounded task-route audit, not proof about every repo link.

Report the broken route, chosen owner, files changed, verification performed,
and remaining uncertainty. Keep the final answer proportional to the change.
If neither pointers nor copied values were wrong, report that result; do not
manufacture a new index, manifest, login flow or test suite to justify the audit.
