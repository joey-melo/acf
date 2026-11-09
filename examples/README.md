# Example agent resources

This directory contains reusable starting points for ACF environments. It is a
reference library, not a source of instructions automatically executed by ACF.
The active checkout resources live in `../prompts/` and `../skills/`; their initial
contents were copied from these examples as requested by this project's setup.

- `prompts/BUG_BOUNTY.md` supplies a scoped bug bounty workflow. Customize it for
  the work you authorize; it requires scope files in each environment.
- `skills/custom/reporting.md` is a reporting skill. ACF accepts standalone
  Markdown skills and converts them to a `reporting/SKILL.md` package when syncing.
- `skills/vendor/caido-mode/` is the public skill from
  [Caido's official repository](https://github.com/caido/skills).
- `skills/vendor/ghidra/` is the community Ghidra headless analysis skill from
  [Armin Ronacher's agent-stuff repository](https://github.com/mitsuhiko/agent-stuff).
  It is not an NSA-authored Ghidra skill.

Vendor snapshots include their supporting scripts, license, provenance, and
exact revisions. `skills/vendor/manifest.json` records upstream file SHA-256
hashes. The Caido skill needs Node.js and its package dependencies (`npm ci` in
its directory). The Ghidra skill needs Ghidra and a compatible Java runtime;
ACF's environment provides installation helpers.

To reuse an example, copy its file or directory into the matching active
`prompts/` or `skills/` directory. Do not put secrets here. Add non-sensitive
placeholder values in `config.toml`; store API keys in its `[credentials]` table
and the dedicated SSH key's path in `oob.ssh_key_path`. The active directories are private local configuration and ignored
by Git; these examples stay versioned.

Prompts are combined in sorted relative-path order. Checkout defaults are
followed by `ACF_HOME/shared` resources and then project resources under
`ACF_HOME/projects/<name>`; a later prompt with the same relative path, or skill
with the same name, overrides the earlier one. `AGENTS.md` instructions combine
in that order. Press `u` in the dashboard to synchronize the selected project,
or use its actions menu to synchronize every running project. Starting a project
or launching an agent from the dashboard also refreshes resources. Start a fresh
agent session to load changed instructions. Open the dashboard with `./acf`;
endpoint authentication uses `./acf login codex` or `./acf login claude`.
