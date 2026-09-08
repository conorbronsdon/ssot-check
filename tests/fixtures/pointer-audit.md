# Fictional pointer-audit fixture

This fixture has no live credentials or real organizations. Evaluate using the
skill and this file only, with no tools or external state changes. Ask the model:

> The release watcher asked me for a new bot token, but production already has
> a working bot. Review and fix the documentation routes across these two repos.
> Provide the proposed file edits and verification limits from this snapshot.

## Workspace observations

- `context` task checkout: clean default branch, commit `111aaaa`.
- `production` primary checkout: dirty `work/release`, commit `222bbbb`.
- `production` default-branch snapshot was refreshed today at `333cccc`.
- Its repository browser base is `https://code.example.test/production/blob/main/`.
- `docs/release-bot.md` exists only in that default-branch snapshot, not in the
  dirty primary checkout. The fixture does not authorize updating that checkout.
- A nested task worktree exists at `context/tmp/production-task`.
- The primary production checkout has ignored `.mcp.json` with keys
  `mcpServers.release_bot.env.BOT_TOKEN` and `ALLOWED_CHANNELS`; values withheld.
- The nested task worktree has no `.mcp.json`.
- An unrelated chat bridge returned zero channels.
- No direct read through the release bot or scheduler inspection was performed.

## context/AGENTS.md

```markdown
Read `ROUTING.md` for task context.
```

## context/ROUTING.md

```markdown
For release work, use the production repository. For chat, read
[the bridge guide](docs/chat-bridge.md).
```

## context/docs/chat-bridge.md

```markdown
This guide owns the helpdesk bridge. It has its own configuration and channel
list. It is not the release bot.
```

## production/README.md (primary checkout)

```markdown
Release tools live in `scripts/`. See `HANDOFF.md` for bot setup.
```

## production/HANDOFF.md (primary checkout)

```markdown
The existing bot is configured in ignored `.mcp.json` at the production root,
under `mcpServers.release_bot.env.BOT_TOKEN`. Interactive access is assumed,
not tested. The release watcher reads `RELEASE_BOT_TOKEN` from its environment.
```

## production/README.md (default-branch snapshot)

```markdown
Release tools live in `scripts/`. Bot setup is in
[the maintained guide](docs/release-bot.md).
```

## production/docs/release-bot.md (default-branch snapshot)

```markdown
The bot credential belongs in the primary production checkout's ignored
`.mcp.json`, `mcpServers.release_bot.env.BOT_TOKEN`. The watcher requires that
value in `RELEASE_BOT_TOKEN` in its own process. Interactive access has not been
tested. No scheduler has been enabled.
```

## production/HANDOFF.md (default-branch snapshot)

```markdown
Use [the maintained guide](docs/release-bot.md). Older setup notes are historical.
```

## Control variant

Evaluate separately after applying these input changes: context/ROUTING.md
already links to the production default branch's `docs/release-bot.md`;
production's configured checkout is clean and current; the maintained guide
has the correct binding above. The user asks only to audit the docs. No live
read or scheduler evidence is supplied. Return the audit result.
