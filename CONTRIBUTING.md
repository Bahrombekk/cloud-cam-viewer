# Contributing

Thanks for helping! Bug reports, camera compatibility reports and pull requests are
all welcome.

## The most useful contribution: a compatibility report

The protocol varies by camera model and firmware. If `cloudcam` works (or doesn't)
with your camera, open a
[Camera compatibility](https://github.com/Bahrombekk/cloud-cam-viewer/issues/new?template=camera_compat.yml)
issue. It takes two minutes and directly grows the
[supported cameras table](README.md#supported-cameras).

## Development setup

```bash
git clone https://github.com/Bahrombekk/cloud-cam-viewer.git
cd cloud-cam-viewer
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e ".[dev,viewer]"

pytest          # no cloud account or network needed
ruff check .
```

CI runs the same two commands on Linux and Windows (Python 3.10 and 3.13).

## Ground rules

- **No change without a test** in `codec/`, `media/`, `stream_manager.py` or
  `sources/cloud/{proxy,vtm_cache}.py`. These paths fail silently (a corrupted picture, not an exception),
  so tests are the only guard. Many existing tests protect behaviour that is easy to
  forget — read the test's docstring before changing what it asserts.
- **Measure, don't guess.** Performance claims in PRs and in the README come with the
  numbers and how they were measured (camera, codec, cold/warm cache).
- **Never commit secrets.** `config.py`, `cam_keys.json`, `token.json` and
  `feature_code.txt` are git-ignored; keep it that way, and scrub serials/codes from
  logs you paste.
- User-facing text goes through `cloudcam.i18n.t("English", "O'zbekcha")`.
- Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).

## Pull requests

1. Branch from `main`, keep the PR focused on one change.
2. `pytest` and `ruff check .` pass.
3. Describe what changed and how you verified it — on a real camera if the change
   touches streaming.

## Releases (maintainers)

Versions follow [SemVer](https://semver.org/). The version lives only in
`src/cloudcam/__init__.py`.

1. Move the `[Unreleased]` changelog entries under a new `## [X.Y.Z] - YYYY-MM-DD`.
2. Bump `__version__`, commit.
3. `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z` — the Release workflow
   builds the package and publishes the GitHub Release.
