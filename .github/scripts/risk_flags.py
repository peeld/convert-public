"""CI: flag changes in a PR that need line-by-line human review.  Prints markdown.

Usage: python .github/scripts/risk_flags.py <base-ref>      (e.g. origin/main)
Never fails the build: it only labels what the reviewer must read.
"""
import re
import subprocess
import sys

base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
diff = subprocess.run(["git", "diff", "--unified=0", base + "...HEAD"], capture_output=True, text=True, check=True).stdout
names = subprocess.run(["git", "diff", "--name-status", base + "...HEAD"], capture_output=True, text=True, check=True).stdout

flags = []
for line in names.splitlines():
    status, _, path = line.partition("\t")
    if path.endswith(".py") and not path.startswith("tests/") and status[0] in "AM":
        flags.append(("HIGH", "Python %s: `%s`" % ("added" if status[0] == "A" else "changed", path)))
    if path == "convert/tools.json":
        flags.append(("HIGH", "`tools.json` changed (tool download)"))
    if path.startswith((".github/", "tools/")) or path in ("manifest.json", "files.json"):
        flags.append(("HIGH", "Repo/CI/release machinery changed: `%s`" % path))
    if path.endswith(".json") and path.startswith("convert/conversions/"):
        flags.append(("LOW", "Conversion JSON: `%s`" % path))

patterns = {
    r"\b(urllib|requests|http\.client|socket|ftplib|smtplib)\b": "network call",
    r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|pty)\b": "spawns a process",
    r"\b(eval|exec|compile|__import__)\s*\(": "dynamic code execution",
    r"\bhttps?://": "URL",
    r"\b(ctypes|winreg|pickle|marshal)\b": "low-level or unsafe module",
}
cur = ""
for line in diff.splitlines():
    if line.startswith("+++ "):
        cur = line[6:]
    elif line.startswith("+") and not line.startswith("+++"):
        for pat, what in patterns.items():
            if re.search(pat, line):
                flags.append(("HIGH", "%s in `%s`: `%s`" % (what, cur, line[1:].strip()[:100])))

print("## Review flags\n")
if not flags:
    print("Nothing flagged. Still needs the maintainer's review.")
for level in ("HIGH", "LOW"):
    for lv, text in dict.fromkeys(flags):
        if lv == level:
            print("- **%s** %s" % (lv, text))
