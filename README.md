# convert-public

The Python side of PeelConvert: the `convert` package (conversions, ops, tools,
paths, runner, app wiring), `convert_startup.py`, and the tests.

In the app repo this is mounted as the `python/` submodule. Installed apps ship a
bundled copy and can fetch newer signed releases of this repo.

- `AGENTS.md` - how to add or change operations (for coding agents and people)
- `manifest.json` - version of this code and the host API levels it works with
- Tests run with plain Python against a fake PeelCore, no app build needed:

      python -m unittest discover -s tests

Personal-only operations belong in the app's userPython folder, not here.
