# Git workflow

Git is the safety checkpoint and rollback path. Changes land through GitHub pull requests. A commit is not permission to deploy, restart Home Assistant, or call a physical service.

## Before editing

```bash
git status --short --branch
git log -3 --oneline --decorate
python3 scripts/preflight.py --baseline origin/main
```

Prefer a clean tree. Do not mix unrelated edits into one checkpoint.

The preflight is read-only and uses local refs. If the baseline is not an ancestor
of HEAD, select the correct isolated worktree before changing code. Runtime and
release correspondence require separate evidence; see [Agent workflow](agent-environment.md).

## Before committing

Run the narrowest relevant tests, then inspect the tree:

```bash
python scripts/local_checks.py

git diff --check
git diff --stat
git diff -- custom_components tests docs README.md CONTRIBUTING.md
```

Stage only the intended files:

```bash
git add <intended-files>
git diff --cached --name-only
git diff --cached --stat
git diff --cached --check
```

Do not stage:

- `.venv*`, caches, coverage databases, `site/`, or editor files
- Home Assistant `/config` state, backups, registries, databases, or runtime credentials
- passwords, API keys, tokens, private keys, or connection strings

Track `AGENTS.md` after a secret scan. It is project policy, not generic profile memory.

```bash
git commit -m "<short description of the logical change>"
git status --short --branch
git show --stat --summary HEAD
```

## Pull requests

Push a feature branch and open a PR against `main`. CI must pass. Review the diff before merge.

A merge is still only a source checkpoint. Live activation is a separate, explicitly approved step.

## Deployment boundary

Deploy only a verified commit. Record the commit, a backup of the installed component, `ha core check`, readiness, and live readback as separate evidence. A clean Git tree does not replace Home Assistant runtime verification.

```bash
git rev-parse --short HEAD
git show --format=fuller --stat HEAD
```

## Rollback

For an accepted but wrong source change, revert. Do not `git reset --hard` or force-push for routine rollback.

```bash
git log --oneline --decorate -5
git show --stat <commit>
git revert <commit>
```

Rerun local gates. Live Home Assistant rollback also needs the recorded component backup, `ha core check`, and an approved activation step.
