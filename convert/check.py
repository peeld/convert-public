"""python -m convert.check [--app NAME] [--json] [folder ...]

Loads the shipped conversions, the user's, and any folders named here, and
prints every problem.  Exit status 1 if there were any.  Run it from the app's
python folder.  Handy after writing a new conversion.

Outside the app there is no host to say whose user folder to read: give the app's
name (--app PeelConvert) or set PEEL_APP_NAME.

--json prints registry.describe() (every conversion as plain values: inputs, output
name, version) and the problems, as one JSON object, for an app or a script.
"""
import json
import sys

from . import host
from .conversion import registry, user_dir


def main(argv):
    argv = list(argv)
    if "--app" in argv:
        at = argv.index("--app")
        if at + 1 >= len(argv):
            print("--app needs the app's name", file=sys.stderr)
            return 2
        host.configure(argv[at + 1])
        del argv[at:at + 2]
    as_json = "--json" in argv
    if as_json:
        argv.remove("--json")
    try:
        folders = [user_dir(), *argv]
    except host.HostError as exc:
        print("%s (or use --app NAME)" % exc, file=sys.stderr)
        return 2
    registry.load_all(folders)
    if as_json:
        print(json.dumps({"conversions": registry.describe(), "problems": registry.problems}, indent=2))
        return 1 if registry.problems else 0
    for conversion in registry.all():
        print("%-28s %-10s %s" % (conversion.id, conversion.group, conversion.label))
    print("\n%d conversion(s)" % len(registry.all()))
    for problem in registry.problems:
        print("PROBLEM:", problem)
    return 1 if registry.problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
