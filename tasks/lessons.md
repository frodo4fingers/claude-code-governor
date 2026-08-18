# lessons

### Do not declare standard plugin directories in plugin.json
- **What**: `plugin.json` carried `"commands": "./commands"` and `"hooks": "./hooks/hooks.json"`.
  Claude Code refused to load the plugin: *"Duplicate hooks file detected: ./hooks/hooks.json
  resolves to already-loaded file ... The standard hooks/hooks.json is loaded automatically,
  so manifest.hooks should only reference additional hook files."*
- **Why**: `commands/`, `agents/`, `skills/`, `hooks/hooks.json` are discovered by convention.
  Naming them again registers them twice.
- **Rule**: keep `plugin.json` to metadata only (name, version, description, keywords). Use the
  `commands`/`hooks`/`agents` keys only for paths *outside* the conventional layout.

### Validating JSON is not validating a manifest
- **What**: the manifest parsed as valid JSON and I treated that as verified. The schema violation
  only surfaced when Claude Code actually loaded the plugin.
- **Why**: `json.load()` proves syntax, not that the host accepts the structure.
- **Rule**: for host-consumed manifests, verification means the host loading it without error -
  check `/plugin` → Errors, not just the parser.

### An installed plugin is a snapshot copy, not the source directory
- **What**: after fixing `plugin.json` in the repo, the plugin still failed the same way.
  `~/.claude/plugins/cache/governor/governor/0.1.0/` holds a *copy* made at install time
  (different inodes), and `/plugin` reported "already at the latest version (0.1.0)", so
  nothing was re-copied. Even a `source: directory` marketplace copies rather than links.
- **Why**: the cache is keyed by the version in `plugin.json`. Editing files in place changes
  nothing that Claude Code looks at.
- **Rule**: after changing any plugin file, bump `version` and run
  `claude plugin update <plugin>@<marketplace>` (restart to apply). For tight iteration use
  `claude --plugin-dir <path>`, which loads the directory live for one session.

### Use `claude plugin validate` before declaring a plugin done
- **What**: `claude plugin validate <path>` catches manifest errors - it flagged the missing
  `author.name` immediately and would have caught the duplicate hooks declaration too.
- **Rule**: run it as the last step of any plugin change. It is the host's own checker.

### Plugin commands are skills, and skills are namespaced
- **What**: `/governor` reported "Unknown command" even with `--plugin-dir` loading the plugin
  live. `claude plugin details governor@governor` showed the inventory as **Skills (1) governor**
  - there is no "Commands" row. The working invocation was `/governor:governor status`.
- **Why**: `commands/*.md` in a plugin registers as a skill, and a plugin skill is always
  addressed as `<plugin>:<skill>` (same as `/caveman:caveman`). A plugin cannot own a bare
  `/name`; only `~/.claude/commands/<name>.md` can.
- **Rule**: don't promise a bare `/name` for a plugin command. Either accept the namespaced
  form or link the file into `~/.claude/commands/`. And read `claude plugin details` after
  installing - it states exactly what the host registered, rather than what you assumed.
