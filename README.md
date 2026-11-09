# convert-public

The file operations of the Peel apps: the `convert` package (conversions, ops, tools,
paths, the run code, the updater) and its tests. It runs in any app that mounts it
(PeelConvert, PeelPost) and asks the app what it needs through `convert/host.py`;
each app keeps its own window glue.

In PeelConvert's repo this is mounted as the `python/` submodule. Installed apps ship a
bundled copy and can fetch newer signed releases of this repo.

- `AGENTS.md` - how to add or change operations (for coding agents and people)
- `manifest.json` - version of this code and the host API levels it works with
- Tests run with plain Python against a fake PeelCore, no app build needed:

      python -m unittest discover -s tests

Personal-only operations belong in the app's userPython folder, not here.

## AI-written conversions

**AI > Connect an AI agent...** turns on a local MCP server and shows what to paste into Claude Code,
Cursor, VS Code, Claude Desktop or Codex so the AI can write, test and save conversions for you. They
are saved in your own `userPython` folder and stay on your computer. See `AGENTS.md` section 9.
