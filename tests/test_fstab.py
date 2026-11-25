import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "builder"))
from filesystems import patch_fstab


class FstabCandidatesTest(unittest.TestCase):
    def test_modes_and_encryption_are_independent(self):
        mounts = ("system", "product", "system_ext")
        stock = b"".join(f"{p} /{p} erofs ro,nodev wait,slotselect,logical,first_stage_mount,avb\n".encode()
                         for p in mounts)
        stock += b"vendor /vendor erofs ro wait,logical\n/dev/data /data f2fs nosuid wait,fileencryption=aes\n"
        for target in (None, "ext4", "f2fs"):
            for decrypt in (False, True):
                for avb in (False, True):
                    with self.subTest(target=target, decrypt=decrypt, avb=avb):
                        formats = dict.fromkeys(mounts, target) if target else {}
                        out = patch_fstab(stock, avb, decrypt, formats)
                        self.assertEqual(out, patch_fstab(out, avb, decrypt, formats))
                        for mount in mounts:
                            rows = [line.split() for line in out.decode().splitlines() if line.startswith(mount+" ")]
                            self.assertCountEqual([r[2] for r in rows], ["ext4", "erofs", "f2fs"])
                            self.assertEqual(rows[0][2], target or "erofs")
                            for row in rows:
                                self.assertEqual(row[3], ("rw" if row[2] == target else "ro") + ",nodev")
                                self.assertEqual(row[4], "wait,slotselect,logical,first_stage_mount" +
                                                 ("" if avb else ",avb"))
                        self.assertEqual(b"fileencryption=" in out, not decrypt)
                        self.assertEqual(out.count(b"vendor /vendor"), 1)

    def test_preserve_oem_options_comments_and_distinct_mount_stages(self):
        stock = (b"\tsystem /system ext4 ro,barrier=1,discard wait,logical,first_stage_mount # ext4\r\n"
                 b"vendor /vendor erofs ro wait,logical\r\n"
                 b"system /system erofs ro,noatime wait,logical,first_stage_mount # erofs\r\n"
                 b"system /system f2fs ro,atgc wait,logical,latemount,fscompress\r\n"
                 b"# system /system ext4 ro wait,logical\r\n"
                 b"other /system ext4 ro wait")
        out = patch_fstab(stock, False, False, {"system": "f2fs"})
        rows = out.split(b"\r\n")
        self.assertEqual(rows[:3], [
            b"\tsystem /system f2fs rw,discard wait,logical,first_stage_mount",
            stock.splitlines()[0], stock.splitlines()[2]])
        self.assertIn(b"system /system f2fs rw,atgc wait,logical,latemount,fscompress", rows)
        self.assertIn(b"system /system erofs ro wait,logical,latemount", rows)
        self.assertEqual(rows[-2:], stock.split(b"\r\n")[-2:])
        self.assertEqual(out, patch_fstab(out, False, False, {"system": "f2fs"}))
        self.assertEqual(patch_fstab(stock, False, False), stock)
