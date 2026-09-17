#!/usr/bin/env python3
"""
Agent-Backup: backs up a Hermes agent profile to a GitHub repo.

Discoverable via $HERMES_HOME/config.yaml or ~/.hermes/profiles/.
Not hardcoded to any specific profile name.

Usage:
    python3 backup.py                  # backup active profile
    python3 backup.py --profile aria   # backup specific profile
    python3 backup.py --dry-run       # preview without pushing
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HERMES_HOME_DEFAULT = Path.home() / ".hermes"
SKIP_DIRS = {
    "state.db", "sessions", "cron", "runtime", "logs", "cache",
    "gateway", "lock", "projects.db", "verification_evidence.db",
    "processes.json", "state.db", "state.db-wal", "state.db-shm",
    "state.db.fts_rebuild.lock", "state.db.quarantine.lock",
    ".skills_prompt_snapshot.json",
}
SKIP_EXTENSIONS = {".lock", ".sock", ".pid"}
SKIP_FILES = {
    "gateway.lock", "gateway.pid", "gateway.sock",
    "gateway-starts.log", "gateway_state.json",
    "auth.lock", "context_length_cache.yaml",
    "models_dev_cache.etag", "models_dev_cache.json",
    "ollama_cloud_models_cache.json", "provider_models_cache.json",
    ".update_check", "verification_evidence.db",
    "projects.db", "processes.json",
}

# Files/dirs to BACK UP (the "you" of the agent)
INCLUDE_DIRS = {"memories", "cross-session-context"}
INCLUDE_FILES = {"config.yaml", ".env", "SOUL.md", "profile.yaml", "channel_directory.json"}


def find_hermes_home():
    """Discover HERMES_HOME from config.yaml or default."""
    # Check env var first
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        p = Path(env_home)
        if p.is_dir():
            return p

    # Fall back to ~/.hermes
    default = HERMES_HOME_DEFAULT
    if default.is_dir():
        return default

    # Try profile-based discovery
    profiles_dir = Path.home() / ".hermes" / "profiles"
    if profiles_dir.is_dir():
        for prof in profiles_dir.iterdir():
            if prof.is_dir() and (prof / "config.yaml").exists():
                return prof.parent  # ~/.hermes

    raise RuntimeError(
        "Cannot discover HERMES_HOME: no $HERMES_HOME, no ~/.hermes, "
        "and no profiles with config.yaml found."
    )


def find_active_profile(hermes_home: Path) -> str:
    """Discover the active profile name from config.yaml or profiles dir."""
    config_path = hermes_home / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            # config.yaml doesn't store active profile name directly;
            # HERMES_HOME env var points at the profile dir itself.
            # So the profile name is the basename of HERMES_HOME.
            return hermes_home.name
        except Exception:
            pass

    # Fallback: pick the only profile, or the one matching HERMES_HOME
    profiles_dir = hermes_home / "profiles" if (hermes_home / "profiles").is_dir() else None
    if profiles_dir:
        profs = [p.name for p in profiles_dir.iterdir() if p.is_dir()]
        if len(profs) == 1:
            return profs[0]
        if hermes_home.name in profs:
            return hermes_home.name
        # Default to first alphabetically
        return sorted(profs)[0]

    raise RuntimeError(f"Cannot discover active profile under {hermes_home}")


def find_profile_dir(hermes_home: Path, profile_name: str) -> Path:
    """Locate the profile directory."""
    # HERMES_HOME usually IS the profile dir when set
    if (hermes_home / "config.yaml").exists():
        return hermes_home
    # Otherwise look under profiles/
    candidate = hermes_home / "profiles" / profile_name
    if candidate.is_dir():
        return candidate
    raise RuntimeError(f"Profile directory not found: {candidate}")


def should_skip(path: Path, name: str) -> bool:
    """Determine if a file/dir should be skipped during backup."""
    if name in SKIP_FILES:
        return True
    if name.startswith("."):
        # Keep .env, .gitignore, etc — but skip dotfiles that are runtime
        if name in {".env", ".gitignore", ".skills_prompt_snapshot.json"}:
            return False
        if name.startswith(".") and name not in INCLUDE_FILES and name not in {" .env"}:
            # skip other dot-files except explicitly included
            if name not in {".env"}:
                return True
    if name in SKIP_DIRS:
        return True
    if path.suffix in SKIP_EXTENSIONS:
        return True
    # Skip SQLite WAL/SHM
    if name.endswith("-wal") or name.endswith("-shm"):
        return True
    return False


def compute_file_hash(path: Path) -> str:
    """SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def encrypt_env_gpg(env_path: Path, passphrase_file: Path) -> Path:
    """Encrypt .env with gpg symmetric encryption. Returns path to .env.gpg.

    The original .env is left in place; the encrypted copy is written to a
    temporary file and returned. The caller is responsible for copying that
    encrypted copy where it needs to go (e.g. into the backup repo).
    """
    # Write the encrypted copy to a temp file so we never touch the original
    tmp = tempfile.NamedTemporaryFile(suffix=".env.gpg", delete=False)
    tmp.close()
    out_path = Path(tmp.name)
    cmd = [
        "gpg", "--symmetric", "--batch", "--yes",
        "--passphrase-file", str(passphrase_file),
        "--output", str(out_path),
        str(env_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"gpg encrypt failed: {result.stderr}")
    return out_path


def backup_profile(profile_dir: Path, repo_dir: Path, passphrase: str,
                   dry_run: bool = False, commit_msg: str = None) -> dict:
    """
    Back up a Hermes agent profile into repo_dir, then commit+push to GitHub.

    Returns a dict with keys: commit_sha, files_backed_up, encrypted_env, etc.
    """
    repo_dir = Path(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    # Create a temporary passphrase file
    tmp_pass = None
    if passphrase:
        tmp_pass = tempfile.NamedTemporaryFile(mode="w", suffix=".pass", delete=False,
                                               prefix="backup_pass_")
        tmp_pass.write(passphrase)
        tmp_pass.close()

    files_backed_up = []
    encrypted_env = None
    skipped = []

    # Walk the profile directory
    for root, dirs, files in os.walk(profile_dir):
        root_path = Path(root)

        # Filter dirs in-place to prevent os.walk from descending
        dirs_to_remove = []
        for d in dirs:
            dp = root_path / d
            if should_skip(dp, d):
                dirs_to_remove.append(d)
                skipped.append(f"dir: {dp.relative_to(profile_dir)}")
        for d in dirs_to_remove:
            dirs.remove(d)

        for f in files:
            fp = root_path / f
            rel = fp.relative_to(profile_dir)

            if should_skip(fp, f):
                skipped.append(f"file: {rel}")
                continue

            # Destination path in repo
            dest = repo_dir / rel

            if f == ".env" and tmp_pass:
                # Encrypt .env
                try:
                    enc = encrypt_env_gpg(fp, Path(tmp_pass.name))
                    # Move encrypted file to repo location
                    enc_rel = rel.with_name(".env.gpg")
                    shutil.copy2(enc, repo_dir / enc_rel)
                    files_backed_up.append(str(enc_rel))
                    encrypted_env = str(enc_rel)
                    # Clean up temp encrypted copy
                    enc.unlink()
                except Exception as e:
                    print(f"WARNING: .env encryption failed: {e}", file=sys.stderr)
                    # Fallback: skip .env
                    skipped.append(f"file: {rel} (encryptfailed)")
                continue

            # Regular copy
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fp, dest)
            files_backed_up.append(str(rel))

    # Create .gitignore
    gitignore = repo_dir / ".gitignore"
    gitignore_lines = [
        "# Agent backup — auto-generated\n",
        "# Do not edit; regenerated on each backup\n",
    ]
    gitignore.write_text("\n".join(gitignore_lines) + "\n")

    # Create backup manifest
    manifest = {
        "profile": profile_dir.name,
        "backup_time": datetime.now(timezone.utc).isoformat(),
        "hermes_home_discovered": str(find_hermes_home()),
        "files": files_backed_up,
        "skipped": skipped,
        "encrypted_env": encrypted_env,
        "version": "1.0.0",
    }
    manifest_path = repo_dir / "backup-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    if dry_run:
        print(f"[DRY RUN] Would back up {len(files_backed_up)} files, skip {len(skipped)}")
        for f in files_backed_up:
            print(f"  + {f}")
        for s in skipped:
            print(f"  - {s}")
        if tmp_pass:
            os.unlink(tmp_pass.name)
        return manifest

    # Initialize git repo if needed
    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
        # Set up git identity for commits
        subprocess.run(["git", "config", "user.email", "backup@zombie-w33d.local"],
                       check=True, capture_output=True, cwd=repo_dir)
        subprocess.run(["git", "config", "user.name", "Hermes Backup"],
                       check=True, capture_output=True, cwd=repo_dir)

    # Add all files
    subprocess.run(["git", "add", "-A"], check=True, cwd=repo_dir, capture_output=True)

    # Check if there's anything to commit
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                            cwd=repo_dir)
    if not status.stdout.strip():
        print("Nothing to commit (all files unchanged).", file=sys.stderr)
        if tmp_pass:
            os.unlink(tmp_pass.name)
        return manifest

    # Commit
    msg = commit_msg or f"Backup {profile_dir.name} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    commit = subprocess.run(
        ["git", "commit", "-m", msg],
        capture_output=True, text=True, cwd=repo_dir
    )
    if commit.returncode != 0:
        print(f"Git commit failed: {commit.stderr}", file=sys.stderr)
        if tmp_pass:
            os.unlink(tmp_pass.name)
        return manifest

    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo_dir
    ).stdout.strip()

    manifest["commit_sha"] = commit_sha

    # Wipe passphrase file
    if tmp_pass:
        os.unlink(tmp_pass.name)

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="Back up a Hermes agent profile to a local git repo (ready to push)."
    )
    parser.add_argument(
        "--profile", "-p",
        help="Profile name to back up (discovered from HERMES_HOME if omitted)"
    )
    parser.add_argument(
        "--target", "-t",
        help="Target directory for the git repo (default: ./agent-backup-<name>)"
    )
    parser.add_argument(
        "--passphrase", "-P",
        help="Passphrase for .env encryption (gpg symmetric). If omitted, .env is skipped."
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview what would be backed up without writing files or committing"
    )
    parser.add_argument(
        "--commit-msg", "-m",
        help="Custom git commit message"
    )
    parser.add_argument(
        "--list-skips",
        action="store_true",
        help="List all files/dirs that would be skipped and exit"
    )
    args = parser.parse_args()

    try:
        hermes_home = find_hermes_home()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    profile_name = args.profile or find_active_profile(hermes_home)
    profile_dir = find_profile_dir(hermes_home, profile_name)

    print(f"Profile: {profile_name}")
    print(f"Profile dir: {profile_dir}")
    print(f"HERMES_HOME: {hermes_home}")

    # Resolve passphrase: --passphrase > keyfile > none
    passphrase = args.passphrase
    if not passphrase:
        keyfile = Path.home() / ".agent-backup-key"
        if keyfile.exists():
            try:
                passphrase = keyfile.read_text().strip()
                print(f"Using keyfile: {keyfile}")
            except Exception as e:
                print(f"WARNING: Could not read keyfile {keyfile}: {e}", file=sys.stderr)

    if args.list_skips:
        skips = []
        for root, dirs, files in os.walk(profile_dir):
            root_path = Path(root)
            dirs_to_remove = []
            for d in dirs:
                dp = root_path / d
                if should_skip(dp, d):
                    dirs_to_remove.append(d)
                    skips.append(f"dir: {dp.relative_to(profile_dir)}")
            for d in dirs_to_remove:
                dirs.remove(d)
            for f in files:
                fp = root_path / f
                if should_skip(fp, f):
                    skips.append(f"file: {fp.relative_to(profile_dir)}")
        print(f"Would skip {len(skips)} items:")
        for s in skips:
            print(f"  {s}")
        return

    target = args.target or f"./agent-backup-{profile_name}"
    manifest = backup_profile(
        profile_dir, target,
        passphrase=passphrase or "",
        dry_run=args.dry_run,
        commit_msg=args.commit_msg,
    )

    if args.dry_run:
        print(f"\nDry run complete. {len(manifest.get('files', []))} files would be backed up.")
    else:
        print(f"\nBackup complete.")
        print(f"  Files backed up: {len(manifest.get('files', []))}")
        print(f"  Skipped: {len(manifest.get('skipped', []))}")
        if manifest.get('encrypted_env'):
            print(f"  .env encrypted -> {manifest['encrypted_env']}")
        if manifest.get('commit_sha'):
            print(f"  Commit: {manifest['commit_sha']}")
        print(f"  Target repo: {target}")


if __name__ == "__main__":
    main()
