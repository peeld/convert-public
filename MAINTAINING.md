# Maintaining convert-public

For the maintainer. Contributors do not need this file.

## One-time GitHub settings

- **Branch protection on `main`** (Settings > Branches, or the command below): require a pull
  request, require 1 approving review, require review from Code Owners, dismiss stale
  approvals, require the `test`, `agreement` and `risk-flags` checks, apply to admins, block
  force pushes and deletion. `.github/CODEOWNERS` makes you the owner of everything, so no
  pull request, including from collaborators, merges without your approval.
- **Tag protection**: restrict who can create `v*` tags to you (Settings > Rules > Rulesets).
- **Actions**: workflows from forks need approval; default workflow token permissions read-only.
- **Optional**: install the CLA Assistant GitHub app so each contributor's agreement is
  recorded once.

      gh api -X PUT repos/peeld/convert-public/branches/main/protection --input - <<'JSON'
      {
        "required_status_checks": {"strict": true, "contexts": ["test", "agreement", "risk-flags"]},
        "enforce_admins": true,
        "required_pull_request_reviews": {"required_approving_review_count": 1,
                                          "require_code_owner_reviews": true,
                                          "dismiss_stale_reviews": true},
        "restrictions": null,
        "allow_force_pushes": false,
        "allow_deletions": false
      }
      JSON

  (`risk-flags` only runs for branches inside this repository, not forks; if you want it
  required, drop it from `contexts` or run it for forks with a `pull_request_target`
  workflow that never checks out fork code.)

## Making a release

1. Bump `version` in `manifest.json` (and `host_api` if needed).
2. `python tools/build_release.py --listing`, then commit `manifest.json` and `files.json`.
3. Tag `vX.Y.Z` and push the tag. The release workflow checks the tag matches the manifest,
   runs the tests and validation, and creates a **draft** release with `convert-X.Y.Z.zip`
   and `files.json`.
4. On the release PC, download `files.json` from the draft and sign it:
   `python tools/sign_release.py sign <everyday.key> files.json`. Attach the resulting
   `files.json.sig` to the draft. Check the three asset names, then publish.

Apps check the signature against the two public keys compiled in; nothing else makes an
update acceptable.

## Keys

- Make two pairs with `python tools/sign_release.py genkey everyday` and `... genkey spare`.
- Public keys go in the app repo's `licenseConfig.h` (`kUpdateKeyEveryday`, `kUpdateKeySpare`,
  `#define PEEL_HAS_UPDATE_KEYS 1`). That file is not in git; back it up by hand.
- The everyday private key lives on the release PC, outside any repo. The spare lives offline.
- Never put a private key in `licenseConfig.h` or in this repository.
- If the everyday key is lost or exposed: sign one update with the spare that switches
  installed apps to a new everyday key (a new installer with the new key follows).

## Testing the update path

Publish a deliberately broken release as a draft-to-prerelease on a test copy of the repo (or
with a test key and a test build): a version whose `convert_startup.py` raises on import.
Expected: the app installs it, fails to start with it, falls back to the previous version on
the same run's next start, records the version as bad, and never offers it again.
