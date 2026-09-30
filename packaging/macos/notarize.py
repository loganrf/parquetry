"""Notarize signed macOS release files with Apple and staple the tickets.

Usage: python packaging/macos/notarize.py PATH...

Each PATH is an .app bundle (uploaded as a zip) or a signed .dmg. The script
submits it to Apple's notary service, waits for the result, prints the
notarization log, staples the ticket to PATH and checks it with Gatekeeper.

Credentials come from the environment, either an App Store Connect API key:

    APPLE_API_KEY_PATH   path to the AuthKey_<id>.p8 file
    APPLE_API_KEY_ID     the key's ID
    APPLE_API_ISSUER_ID  issuer ID (team keys only; leave unset for individual keys)

or a keychain profile saved with `xcrun notarytool store-credentials`:

    APPLE_NOTARY_PROFILE profile name

See docs/macos-notarization.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def credentials() -> list[str]:
    if profile := os.environ.get("APPLE_NOTARY_PROFILE"):
        return ["--keychain-profile", profile]
    key, key_id = os.environ.get("APPLE_API_KEY_PATH"), os.environ.get("APPLE_API_KEY_ID")
    if not key or not key_id:
        raise SystemExit("set APPLE_API_KEY_PATH and APPLE_API_KEY_ID, or APPLE_NOTARY_PROFILE")
    args = ["--key", key, "--key-id", key_id]
    if issuer := os.environ.get("APPLE_API_ISSUER_ID"):
        args += ["--issuer", issuer]
    return args


def run(*args: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(str(a) for a in args), flush=True)
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    print(result.stdout + result.stderr, flush=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed with exit code {result.returncode}")
    return result


def submit(upload: Path, auth: list[str]) -> None:
    result = run(
        "xcrun", "notarytool", "submit", upload, *auth, "--wait", "--timeout", "30m", "--output-format", "json",
        check=False,
    )
    try:
        submission = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"notarytool did not report a result (exit code {result.returncode})") from None
    # The log lists every problem Apple found, and warnings even when accepted.
    if "id" in submission:
        run("xcrun", "notarytool", "log", submission["id"], *auth, check=False)
    if submission.get("status") != "Accepted":
        status, message = submission.get("status"), submission.get("message", "")
        raise SystemExit(f"notarization of {upload.name} failed: {status} {message}")


def notarize(path: Path, auth: list[str]) -> None:
    if path.suffix == ".app":
        run("codesign", "--verify", "--deep", "--strict", "--verbose=2", path)
        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp) / f"{path.stem}.zip"
            run("ditto", "-c", "-k", "--keepParent", path, upload)
            submit(upload, auth)
        assessment = ["--type", "execute"]
    elif path.suffix == ".dmg":
        run("codesign", "--verify", "--strict", "--verbose=2", path)
        submit(path, auth)
        assessment = ["--type", "open", "--context", "context:primary-signature"]
    else:
        raise SystemExit(f"cannot notarize {path}: expected an .app or a .dmg")
    run("xcrun", "stapler", "staple", path)
    run("xcrun", "stapler", "validate", path)
    run("spctl", "--assess", *assessment, "--verbose=2", path)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("notarization needs macOS (xcrun notarytool)")
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths:
        raise SystemExit(__doc__)
    auth = credentials()
    for path in paths:
        notarize(path.resolve(), auth)
    print("notarized:", ", ".join(path.name for path in paths))


if __name__ == "__main__":
    main()
