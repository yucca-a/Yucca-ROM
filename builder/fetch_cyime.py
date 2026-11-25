"""Install the lightweight arm64 CyIME APK from its latest public release."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


LATEST = "https://api.github.com/repos/Cyletix/CyIME/releases/latest"
CURL = ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3",
        "--connect-timeout", "15", "--proto", "=https"]


def select_apk(release):
    candidates = [a for a in release["assets"]
                  if a["state"] == "uploaded" and a["size"] > 0
                  and a["name"].lower().startswith("cyime-")
                  and a["name"].lower().endswith("-arm64-v8a.apk")
                  and "-full-" not in a["name"].lower()]
    if not candidates:
        raise ValueError(f"CyIME {release['tag_name']} has no lightweight arm64-v8a APK")
    return min(candidates, key=lambda a: (a["size"], a["name"]))


def fetch(destination):
    release = json.loads(subprocess.check_output([*CURL, LATEST]))
    asset = select_apk(release)
    print(f"CyIME {release['tag_name']}: {asset['name']} ({asset['size']} bytes)", flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        apk = Path(folder) / "download.apk"
        subprocess.run([*CURL, "--output", apk, asset["browser_download_url"]], check=True)
        with apk.open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        if apk.stat().st_size != asset["size"] or digest != asset["digest"]:
            raise ValueError(f"CyIME size or SHA-256 mismatch: {asset['name']}")
        apk.chmod(0o644)
        apk.replace(destination)
    print(f"Verified {digest}", flush=True)


if __name__ == "__main__":
    fetch(Path(sys.argv[1]))
