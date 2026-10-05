# Contributing to PeelConvert's operations

This repository is the Python side of PeelConvert. You can add or improve conversions and
send them back as a pull request. Nothing reaches other users until the maintainer has
reviewed it, merged it, and signed and published a release.

**Just want an operation for yourself?** You do not need any of this. Put it in your
`userPython` folder (`%APPDATA%\PeelConvert\userPython\conversions\`) and press Reload;
updates never touch it. `AGENTS.md` explains how conversions work, and it is written for
coding agents as much as for people.

## How to contribute

1. Fork this repository and clone your fork. In PeelConvert, use **Help > Developer folder...**
   to point the app at the clone (or let it clone for you). Restart the app.
2. Add your conversion (a JSON file, or a `.py` file when JSON cannot say it) and tests.
   `AGENTS.md` has the recipes. Tests run with plain Python, no app build needed:

       python -m unittest discover -s tests
       PYTHONPATH=tests/fakecore python tools/validate.py

3. Press **Reload** in the app and try the operation on a real file.
4. Push a branch to your fork and open a pull request. The template asks what the operation
   does, which tool it uses, a sample input and the output it should give.

## What gets reviewed, and how closely

Every pull request is reviewed by the maintainer before it is merged, including from
collaborators. Automatic checks run first so the reviewer only sees valid changes, and they
flag what needs a close look.

| Kind of contribution | Risk | What the reviewer does |
|---|---|---|
| JSON conversion using an existing tool | Low: arguments for a tool, nothing run by Python | Reads it |
| Python conversion or new Op | High: code that runs on every user's machine | Reads every line |
| New tool download in `tools.json` | High: fetches and runs a binary | Needs a pinned SHA-256 and a host the maintainer recognises |

Expect questions on anything that makes network calls, starts processes, or runs code it
did not ship with. Keep Python contributions small and boring.

## Contribution terms

By opening a pull request you confirm, and you must tick the box in the pull request
description (a check fails the pull request if it is not ticked):

1. The contribution is your own work, or comes from a source whose license allows it to be
   included in PeelConvert, and you have the right to submit it.
2. You grant Peel Software Development LLC a perpetual, worldwide, irrevocable,
   royalty-free right to use, modify, sublicense and distribute the contribution as part of
   PeelConvert, under the project license (see `LICENSE`), with no restriction that would
   stop it being added to the app.
3. You name any third-party code you copied or tool you add (source and license) in the pull
   request. Anything under terms that would prevent shipping it in a closed-source app,
   such as licenses that forbid redistribution or require the app's source to be released,
   will be declined. The ffmpeg build used is the LGPL one for that reason.

These terms are a working policy and have not yet had legal review; they may be reworded.
If you cannot agree to them, keep your operation in your own `userPython` folder.

## Rules the checks enforce

- Tests pass, and every shipped conversion loads (`tools/validate.py`).
- Each tool download in `tools.json` is `https` with a `sha256` or a `sha256_url`.
- No secrets, keys or license values in this repository, ever.
- Only operations that fit the existing host: a change that needs C++ or a new binding goes
  in the app repository and ships as a new installer.
