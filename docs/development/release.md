# Release

`hacs.json` sets `zip_release` and `hide_default_branch`, so HACS installs and
updates this integration from a GitHub release asset named
`power_orchestrator.zip` and never from `main`. A version with no published
release is a version nobody can install.

## What the pipeline does

A tag matching `v<major>.<minor>.<patch>` starts `.github/workflows/release.yml`,
which refuses to publish unless every gate passes:

| Step | Gate |
| --- | --- |
| `gates` | The full CI workflow: compile, Ruff, mypy, resource validation, unit coverage, real Home Assistant suite |
| `validation` | hassfest and HACS validation against the tagged tree |
| `publish` | The tag must be an ancestor of `main`, and the manifest version must equal the tag without its leading `v` |

`scripts/build_release_zip.py` builds the asset and then inspects it. HACS
extracts the zip directly into `config/custom_components/power_orchestrator/`
without stripping a path prefix, so the build fails if `manifest.json` is not at
the zip root, if any entry starts with `custom_components`, or if `hacs.json`
leaked into the archive. Builds are byte-identical for a given commit, and the
run summary records the file list and the SHA-256 of the published asset.

## Publishing

Bump the version in one commit, on `main`:

- `custom_components/power_orchestrator/manifest.json`
- `pyproject.toml`
- the `"model"` value in `binary_sensor.py`, `select.py`, and `sensor.py`

`tests/test_package_quality.py` fails if any of those disagree, so a half-done
bump cannot merge.

Then tag the reviewed commit and push the tag:

```bash
git tag v0.5.1
git push origin v0.5.1
```

Check the asset locally first if you want the same verification without a tag:

```bash
python scripts/build_release_zip.py --expect-version 0.5.1
```

## Rollback

A release is immutable evidence, so roll forward rather than editing a published
release. Delete the release and its tag only if the asset itself is wrong, then
publish a new patch version. HACS keeps offering the five most recent releases,
so users can also pin the previous version from the HACS download dialog.
