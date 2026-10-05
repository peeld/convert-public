"""CI: fail unless the PR description has the contribution-agreement box ticked.

Reads the PR body from the PR_BODY environment variable.
"""
import os
import re
import sys

body = os.environ.get("PR_BODY", "")
if re.search(r"^\s*[-*]\s*\[[xX]\]\s*I agree to the contribution terms", body, re.M):
    print("Contribution agreement ticked.")
    sys.exit(0)
print("ERROR: tick the 'I agree to the contribution terms' box in the PR description (see CONTRIBUTING.md).")
sys.exit(1)
