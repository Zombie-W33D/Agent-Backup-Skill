# Agent-Backup-Skill

A reusable Hermes agent backup skill + Python backing script.

Backs up "the you" of a Hermes agent profile — config, memories, cross-session context, non-GH profile-local skills, SOUL.md, and .env (gpg-encrypted) — into a git repo ready to push to GitHub.

## What gets backed up

| Category | Items |
|---|---|
| Configuration | `config.yaml` |
| Identity | `SOUL.md` (if present), `profile.yaml` |
| Memory | `memories/`, `cross-session-context/` |
| Skills | Profile-local non-GH skills (directories under `skills/` without their own `.git`) |
| Secrets | `.env` (encrypted with `gpg --symmetric --batch --passphrase-file`) |
| Channel state | `channel_directory.json` |

## What gets skipped

Runtime/state artifacts that shouldn't be versioned:
- `state.db`, `state.db-wal`, `state.db-shm`, lock files
- `sessions/`, `cron/`, `runtime/`, `logs/`, `cache/`, `gateway/`
- `projects.db`, `verification_evidence.db`, `processes.json`
- Lock files (`.lock`, `.sock`, `.pid`)
- Large cache JSONs (`models_dev_cache.json`, etc.)

## Location

This skill lives in three places:

- **Real working copy (where backup.py runs from):** `/bot-skillcode/agent-backup-skill/`

### Running backup.py from its new home

```bash
cd /bot-skillcode/agent-backup-skill
python3 backup.py --target /tmp/backup-test
```

### As a skill (Hermes agent)

When invoked as a Hermes skill, the skill:
1. Discovers the active profile from `$HERMES_HOME/config.yaml` or `~/.hermes/profiles/<profile>`
2. Runs `backup.py` to back up the profile to a local git repo
3. Reports the commit SHA and files backed up

The skill never hardcodes "aria" — it discovers the active profile dynamically.

### Usage

#### Running backup.py

```bash
# Back up the active profile (discovered automatically)
cd /bot-skillcode/agent-backup-skill && python3 backup.py

# Back up a specific profile
cd /bot-skillcode/agent-backup-skill && python3 backup.py --profile aria

# With .env encryption (passphrase via keyfile — auto-reads ~/.agent-backup-key)
cd /bot-skillcode/agent-backup-skill && python3 backup.py

# Dry run — see what would be backed up
cd /bot-skillcode/agent-backup-skill && python3 backup.py --dry-run

# List everything that would be skipped
cd /bot-skillcode/agent-backup-skill && python3 backup.py --list-skips

# Custom target directory
cd /bot-skillcode/agent-backup-skill && python3 backup.py --target /path/to/repo
```

### Pushing to GitHub

After running `backup.py`, push the resulting repo:

```bash
cd agent-backup-aria
git remote add origin git@github.com:Zombie-W33D/Agent-aria-Backup.git
git push -u origin main
```

Or use `gh`:

```bash
gh repo create Zombie-W33D/Agent-aria-Backup --private --source=agent-backup-aria --remote
```

## Encryption

The `.env` file is encrypted using GPG symmetric encryption:

```bash
gpg --symmetric --batch --yes --passphrase-file <passfile> --output .env.gpg .env
```

The plaintext `.env` is removed after encryption. Only `.env.gpg` is stored in the backup repo.

To decrypt:

```bash
gpg --decrypt --batch --yes --passphrase-file <passfile> --output .env .env.gpg
```

## Restoring

Use the [Agent-Backup-Tool](https://github.com/Zombie-W33D/Agent-Backup-Tool) to decrypt and restore a `.env` from a backup repo. It provides both a tkinter GUI and a CLI fallback.

## Requirements

- Python 3.11+
- `pyyaml` (for config.yaml parsing)
- `gpg` (GnuPG 2.4+)
- `git`

Install deps:

```bash
pip install pyyaml
```

## Profile discovery

The script discovers the active profile via:

1. `$HERMES_HOME` environment variable (points at the profile directory)
2. `~/.hermes/profiles/<profile>/config.yaml` (fallback: first profile or the one matching HERMES_HOME)
3. `~/.hermes/config.yaml` (global config, if no profile-specific HERMES_HOME)

This means the skill works for any agent profile — not just "aria".
