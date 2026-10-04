# Releasing cc-swap

This is for maintainers. Users don't need any of it.

## Tags

cc-swap releases are tagged `cc-vX.Y.Z` (`cc-v0.3.1`), never `vX.Y.Z`. The fork inherited upstream's tags (`v0.3.0` ... `v0.26.0`), so a release created as `v0.4.0` would attach to upstream's old `v0.4.0` tag and `cc-swap upgrade` would install upstream's code. `cc-swap upgrade`, `upgrade --check` and the update notice therefore read the fork's releases list and consider only `cc-v` tags. The one exception is a fallback to the four fork tags from before this scheme (`v0.1.0`, `v0.1.1`, `v0.2.0`, `v0.3.0`), used only while no `cc-v` release exists. No other `v*` tag is ever used.

## Making a release

Bump `version` in `pyproject.toml`, run `uv lock`, merge that to `main`, then from an up-to-date `main`:

```bash
uv run python tools/release.py 0.3.1 --dry-run   # every check, no changes
uv run python tools/release.py 0.3.1
```

The script refuses (exit 1, `release refused: …` on stderr, saying what to fix) unless:

- the `pyproject.toml` version is the one you passed,
- the working tree is clean,
- you are on `main` and level with the fork's `main` (it fetches the fork's `main` and compares),
- tag `cc-v0.3.1` exists neither locally nor on the fork's remote (checked with `git ls-remote --tags`),
- and `uv run pytest -q` passes.

It then creates an annotated tag at `HEAD`, pushes it to the remote that points at `wonjun-lab/cc-swap` (found by URL, since `origin` may be upstream), and runs `gh release create cc-v0.3.1 --verify-tag --repo wonjun-lab/cc-swap` (with the title `cc-swap 0.3.1` and generated notes). `--verify-tag` makes `gh` fail rather than reuse or create a tag anywhere else. Do not create fork releases by hand with `gh release create`.

## Documentation tests

`README.md` and `docs/reference.md` are checked against the code by `tests/maximize/test_readme.py`, `tests/maximize/test_why.py`, `tests/maximize/test_ride_surfaces.py` and `tests/maximize/test_claude_update.py`: every setting and its default, every `why` reason code, every fork command and flag, every bound Fleet key, every `CC_SWAP_*` environment variable, and the sentences Fleet prints. When you add a setting, a command, a reason code or a key, document it in `docs/reference.md` or those tests fail.
