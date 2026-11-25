import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github/scripts"))
import release


class DeviceReleaseTest(unittest.TestCase):
    def test_xiaomi_tag(self):
        info = dict(model="Xiaomi 17 Pro Max", device="popsicle", version="v1", channel="stable", build="1-1")
        tag, title, archive = release.identity(info)
        self.assertEqual(archive, "YuccaROM_popsicle_v1_stable_r1-1.zip")
        self.assertEqual(title, "Xiaomi 17 Pro Max (popsicle)")
        self.assertEqual(tag, "Xiaomi-17-Pro-Max-(popsicle)")

    def test_hyperos_release_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.zip"
            env = dict(DEVICE="popsicle", CODENAME="popsicle", DEVICE_NAME="Xiaomi 17 Pro Max",
                       BRAND="xiaomi", CHANNEL="stable", GITHUB_RUN_ID="42", GITHUB_RUN_ATTEMPT="1",
                       ARCHIVE=str(source))
            cases = (
                ("OS4.0.0.35.XPBCNXM", "OS4.0.0.35", "true", "true", "encrypted"),
                ("OS4.0.0.35.XPBCNXM", "OS4.0.0.35", "true", "false", "decrypted"),
                ("OS3.0.1.0.WPBEUXM", "OS3.0.1.0", "false", "false", "encrypted"),
                ("OS4.0.0.35", "OS4.0.0.35", "false", "true", "encrypted"),
                ("custom", "custom", "TRUE", "false", "decrypted"),
            )
            for stock, display, remove, keep, variant in cases:
                with self.subTest(stock=stock, variant=variant), patch.dict(
                    os.environ, env, VERSION=stock, REMOVE_ENCRYPT=remove, KEEP_ENCRYPTION=keep
                ):
                    source.write_bytes(b"rom")
                    folder = root / f"{stock}_{variant}"
                    release.stage(folder)
                    info = json.loads((folder / "build.json").read_text(encoding="utf-8"))
                    self.assertEqual(info.get("stock_version", info["version"]), stock)
                    name = f"YuccaROM_popsicle_{display}_stable_{variant}.zip"
                    links = []
                    for asset, _ in release.assets(folder / name, root, limit=3, part_size=2):
                        links.append(asset.name)
                        if asset.suffix == ".sha256":
                            self.assertEqual(asset.read_text().split()[1], name)
                    stem = Path(name).stem
                    self.assertEqual(links, [f"{stem}-1.zip", f"{stem}-2.zip", name + ".sha256"])
                    entry = release.add_entry(release.INDEX, info, links, name)
                    self.assertIn(f"**{display}**", entry)
                    self.assertIn("**加密Data**" if variant == "encrypted" else "**解密Data**", entry)
                    self.assertNotIn("42-1", entry)
                    if stock != display:
                        self.assertNotIn(stock, entry)

    def test_split_boundary_and_whole_file_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "test.zip"
            for size in (1, 9, 10, 11, 12, 65):
                with self.subTest(size=size):
                    archive.write_bytes(bytes(range(size)))
                    chunks = []
                    for asset, digest in release.assets(archive, root, limit=10, part_size=6):
                        data = asset.read_bytes()
                        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                        if asset.suffix == ".sha256":
                            whole = hashlib.sha256(b"".join(chunks)).hexdigest()
                            self.assertEqual(data.decode(), f"{whole}  test.zip\n")
                        else:
                            self.assertEqual(asset.name, "test.zip" if size < 10
                                             else f"test-{len(chunks) + 1}.zip")
                            self.assertLess(len(data), 10)
                            chunks.append(data)
                    self.assertEqual(b"".join(chunks), archive.read_bytes())

    def test_publish_resume_and_append(self):
        releases = []
        uploads = {}
        interrupted = True

        def listing(endpoint, _):
            return list(uploads.values()) if endpoint.endswith("/assets") else releases

        def request(method, _, **fields):
            if method == "POST":
                self.assertFalse(releases)
                releases.append(dict(id=10, **fields))
                raise json.JSONDecodeError("lost create response", "", 0)
            else:
                self.assertEqual(method, "PATCH")
                releases[0].update(fields)
            return releases[0]

        def upload(cmd, action, tag, file, *_):
            nonlocal interrupted
            self.assertEqual((cmd, action, tag), ("release", "upload", "Samsung-Galaxy-S24+-(e2q)"))
            path = Path(file)
            self.assertNotIn(path.name, uploads)
            uploads[path.name] = dict(id=len(uploads), name=path.name, size=path.stat().st_size,
                                      state="uploaded", digest="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
            if path.suffix == ".sha256" and interrupted:
                interrupted = False
                raise ConnectionError("HTTP 502 after upload")

        with tempfile.TemporaryDirectory() as tmp, patch.object(release, "list_entries", listing), \
                patch.object(release.time, "sleep") as sleep, \
                patch.object(release, "ASSET_LIMIT", 2), patch.object(release, "PART_SIZE", 1), \
                patch.object(release, "request", request), patch.object(release, "gh", upload):
            root = Path(tmp)
            source = root / "temp.zip"
            env = dict(DEVICE="e2q", CODENAME="e2q", DEVICE_NAME="Samsung Galaxy S24+", BRAND="Samsung",
                       CHANNEL="CHC", GITHUB_RUN_ID="5", GH_REPO="owner/repo", GITHUB_SHA="abc",
                       VERSION="S9260TEST", ARCHIVE=str(source))
            with patch.dict(os.environ, env):
                for attempt, remove in ((1, "true"), (2, "false"), (3, "false")):
                    os.environ["GITHUB_RUN_ATTEMPT"] = str(attempt)
                    os.environ["REMOVE_ENCRYPT"] = remove
                    source.write_bytes(remove.encode()[:2])
                    folder = root / str(attempt)
                    release.stage(folder)
                    info = json.loads((folder / "build.json").read_text(encoding="utf-8"))
                    self.assertEqual(release.identity(info)[1], "Samsung Galaxy S24+ (e2q)")
                    if attempt == 2:
                        releases[0].update(draft=True, tag_name="Samsung\u00a0Galaxy\u00a0S24+\u00a0(e2q)")
                    release.publish(folder)
                    self.assertEqual(sleep.call_count, 2)
                    release.publish(folder)
                    self.assertFalse(releases[0]["draft"])
                    self.assertEqual(len(uploads), 3 * min(attempt, 2))
                    self.assertEqual(releases[0]["body"].count("<!-- rom:"), min(attempt, 2))
                    for filename in uploads:
                        self.assertIn(f"/{filename})", releases[0]["body"])
                self.assertIn("/download/Samsung-Galaxy-S24%2B-%28e2q%29/", releases[0]["body"])
                archive_name = release.identity(info)[2]
                uploads[archive_name + ".sha256"]["digest"] = "sha256:bad"
                with self.assertRaisesRegex(ValueError, "Conflicting release asset"):
                    release.publish(folder)


if __name__ == "__main__":
    unittest.main()
