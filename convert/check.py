"""python -m convert.check [folder ...]

Loads the shipped conversions, the user's, and any folders named here, and
prints every problem.  Exit status 1 if there were any.  Run it from the app's
python folder.  Handy after writing a new conversion.
"""
import sys

from .conversion import registry, user_dir


def main(argv):
    registry.load_all([user_dir(), *argv])
    for conversion in registry.all():
        print("%-28s %-10s %s" % (conversion.id, conversion.group, conversion.label))
    print("\n%d conversion(s)" % len(registry.all()))
    for problem in registry.problems:
        print("PROBLEM:", problem)
    return 1 if registry.problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
