# Local Git change workflow

This project uses Git as a **local safety checkpoint and rollback mechanism**. It intentionally has no GitHub repository or other remote requirement.

## Before editing

```bash
git status --short --branch
git log -3 --oneline --decorate
git remote -v
```

A clean working tree is preferred before a new logical change. If existing work is present, first identify the intended scope and do not mix unrelated edits into the checkpoint.

## Before committing

Run the narrowest relevant tests first, then inspect the candidate tree:

```bash
# Example focused test command
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv-quality/bin/python -m pytest tests/test_coordinator.py -q

git diff --check
git diff --stat
git diff -- custom_components tests docs README.md HA_VERIFICATION.md
```

Stage only the intended files and inspect the staged result:

```bash
git add <intended-files>
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
```

Do not stage:

- `.venv*`, caches, coverage databases, build artifacts, or editor files;
- `AGENTS.md`, which is project-local operating policy and intentionally remains outside commits;
- Home Assistant `/config` state, backups, registries, databases, or runtime credentials;
- passwords, API keys, tokens, private keys, or connection strings.

Create one descriptive commit for one coherent change:

```bash
git commit -m "<short description of the logical change>"
git status --short --branch
git show --stat --summary HEAD
git diff HEAD --check
```

A Git commit is only a source checkpoint. It is not permission to deploy, restart Home Assistant, or call a physical service.

## Deployment boundary

Deploy only a verified commit. Before a live replacement, create and hash a remote backup of the current component and record the source commit:

```bash
git rev-parse --short HEAD
git show --format=fuller --stat HEAD
```

Keep the deployed commit, local archive hash, remote backup path/hash, `ha core check`, readiness, and live readback as separate evidence. A clean Git tree does not replace Home Assistant runtime verification.

## Rollback

For an accepted but unsafe or incorrect source change, preserve history with a revert:

```bash
git log --oneline --decorate -5
git show --stat <commit>
git revert <commit>
```

Then rerun the relevant local gates and make a new controlled deployment from the resulting commit. Do not use `git reset --hard`, force-push, or history deletion for routine rollback. Live Home Assistant rollback also requires restoring the separately recorded remote component backup, running `ha core check`, and performing the explicitly approved activation step.

## Repository boundary

The repository is local-only by design. Do not create a GitHub repository or add a remote unless the user explicitly asks for that separate operation:

```bash
git remote -v
```

An empty output is expected.
