# SSOT Check privacy

Effective September 9, 2026. Maintainer: Conor Bronsdon.

SSOT Check is an open-source documentation audit tool and agent skill. It has
no maintainer-operated backend, account system, analytics, or telemetry. The
maintainer does not receive the documents you audit through ordinary use of the
plugin.

## Data used during an audit

The CLI reads the files, manifest entries, and repository metadata needed for
the requested audit. Its reports can include file paths, extracted values,
branch names, commit identifiers, and freshness dates. Those values may include
personal information if it is present in the documents you choose to audit.
Use a narrowly scoped manifest or discovery root, and exclude sensitive files.
Do not use the plugin to audit secrets, government identifiers, payment card
data, or protected health information.

The agent skill can read documentation and propose changes. When you request
repairs, the agent can edit the relevant documents using its host's tools. The
CLI itself does not edit documents or manifests. It prints results to your
terminal or calling process and does not maintain its own audit database.

## Recipients and network access

Your agent host, such as ChatGPT, Codex, or another assistant, may process the
files and reports used in the conversation under that provider's terms,
privacy policy, and your account or workspace settings. This policy does not
set or override the host's retention or training settings.

The CLI makes no network requests by default. Its optional `--fetch` mode runs
Git against your configured remotes to refresh sibling repository references.
The remote provider receives the normal Git connection and authentication
information under its own policies. The documentation pointer workflow may
use your host's connected tools for a relevant, authorized access check. Those
services receive the requests required for that check under their own policies.

## Retention and controls

The maintainer retains no audit content from ordinary plugin execution because
the plugin does not send it to a maintainer-operated service. Your files remain
where you keep them. Terminal logs, saved reports, conversation history, Git
metadata, and backups follow the retention and deletion controls of your own
environment and providers. `--fetch` updates local Git metadata; it does not
delete or replace working-tree files.

You control which repositories and files the audit reads, whether to enable
`--fetch`, whether to authorize edits, and whether to keep reports. You can
remove the installed plugin and use your host's controls to manage saved
conversations and tool access.

## Support

If you open a [GitHub issue](https://github.com/conorbronsdon/ssot-check/issues),
your GitHub identity and the content you post are public and are processed by
GitHub under its privacy policy. Use fictional or redacted examples. Public
issue content may remain in project history; manage your contributions through
GitHub's controls. For a vulnerability, follow the repository's
[security reporting instructions](../SECURITY.md).
