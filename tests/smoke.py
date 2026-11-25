"""Run under sudo unshare -m --propagation private on a Linux filesystem."""
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "builder"))
from boot import disable_xiaomi_hashtree
from build import odin_files
from filesystems import convert_to_f2fs, edit, f2fs_sizes, patch_fstab
from super_image import grow_xiaomi_ext4, layout, repack, run, samsung, stock_layout


def check_ext4_growth(temp, tree):
    images = temp / "growth"
    images.mkdir()
    tree.chmod(0o751)
    os.chown(tree, 0, 123)
    run("mkfs.erofs", images / "product.img", tree)
    run("mkfs.erofs", images / "system.img", tree)
    (images / "opaque.img").write_bytes(bytes(1 << 20))
    parts = {"product_a": ("small", "readonly", "product"),
             "system_a": ("main", "readonly", "system"),
             "opaque_a": ("main", "readonly", "opaque"), "system_b": ("b", "readonly", None)}
    info = layout(64 << 20, {"small": 8 << 20, "main": 0, "b": 0}, parts, 65536, 3, True)
    with patch.dict(os.environ, PRODUCT_A_FS="ext4", SYSTEM_A_FS="ext4"):
        targets = edit(info, images, images, lambda rom: None)
    minimum = {n: (images / (parts[n][2] + ".img")).stat().st_size for n in targets}
    assert set(targets) == {"product_a", "system_a"}
    for limits, message in [({"size": 2 << 20}, "physical super"),
                            ({"groups": dict(info["groups"], small=4096)}, "small")]:
        try:
            grow_xiaomi_ext4(dict(info, **limits), images, targets, images)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError("Accepted an impossible minimum layout")
    grow_xiaomi_ext4(info, images, targets, images)
    assert 7 << 20 < (images / "product.img").stat().st_size <= 8 << 20
    assert (images / "system.img").stat().st_size > 52 << 20
    repack(info, images, images / "super.img", sparse=False)
    for n in targets:
        img = images / (parts[n][2] + ".img")
        mount = images / "inspect"
        mount.mkdir(exist_ok=True)
        run("mount", "-t", "ext4", "-o", "ro,loop", img, mount)
        try:
            st = mount.stat()
            assert (st.st_gid, st.st_mode & 0o777) == (123, 0o751)
            assert os.getxattr(mount, "security.selinux") == os.getxattr(tree, "security.selinux")
            assert (mount / "marker").read_text() == "stock"
            assert img.stat().st_size > minimum[n]
        finally:
            run("umount", mount)


def check(temp):
    # Same content, unequal ext4 slack. Preserve the LP budget and alignment padding.
    alignment = 1 << 20
    small, large = temp / "small.ext4", temp / "large.ext4"
    for path, size in [(small, 16 * alignment + 4096), (large, 64 * alignment)]:
        with path.open("xb") as f:
            f.truncate(size)
        run("mke2fs", "-q", "-t", "ext4", "-b", "4096", path)
    sizes = f2fs_sizes([small, large], alignment)
    assert sizes[small] > small.stat().st_size and sizes[large] < large.stat().st_size
    assert sum(sizes.values()) == small.stat().st_size + large.stat().st_size
    assert all((size - path.stat().st_size) % alignment == 0 for path, size in sizes.items())
    assert f2fs_sizes([small], alignment) == {small: small.stat().st_size}

    # Rebuilding an unsigned vbmeta made popsicle reject an otherwise bootable slot.
    key, vbmeta = temp / "test-key.pem", temp / "vbmeta.img"
    run("openssl", "genrsa", "-out", key, "2048")
    run("avbtool", "make_vbmeta_image", "--algorithm", "SHA256_RSA2048", "--key", key,
        "--rollback_index", "42", "--padding_size", "4096", "--output", vbmeta)
    original = vbmeta.read_bytes()
    expected = original[:123] + b"\x01" + original[124:]
    disable_xiaomi_hashtree(vbmeta)
    assert vbmeta.read_bytes() == expected
    disable_xiaomi_hashtree(vbmeta)
    assert vbmeta.read_bytes() == expected

    src, tree, decoded = (temp / n for n in ("source", "tree", "decoded"))
    for d in (src, tree, decoded):
        d.mkdir()
    marker = tree / "marker"
    root_label = b"u:object_r:rootfs:s0"
    os.setxattr(tree, "security.selinux", root_label)
    marker.write_text("stock")
    marker.chmod(0o750)
    os.chown(marker, 1000, 2000)
    os.setxattr(marker, "security.selinux", b"u:object_r:system_file:s0\0")
    run("mkfs.erofs", src / "product.img", tree)
    run("truncate", "-s", "8M", src / "system.img")
    run("mke2fs", "-q", "-t", "ext4", "-d", tree, src / "system.img")
    opaque = src / "opaque.img"
    opaque.write_bytes(b"ROM!" * 1024)
    parts = {n: ("main", "readonly", n) for n in ("system", "product", "opaque")}
    parts["empty"] = ("main", "none", None)
    info = layout(32 << 20, {"main": 30 << 20}, parts, 65536, 2, False)
    repack(info, src, temp / "super.img", sparse=True)
    run("lz4", "-f", temp / "super.img", temp / "super.img.lz4")
    with tarfile.open(temp / "AP_TEST_CL1.tar", "w") as tar:
        tar.add(temp / "super.img.lz4", arcname="super.img.lz4")
        tar.add(marker, arcname="userdata.img.lz4")
    with tarfile.open(temp / "CSC_TEST.tar", "w") as tar:
        tar.add(marker, arcname="TEST.pit")
    with tarfile.open(temp / "HOME_CSC_TEST.tar", "w") as tar:
        tar.add(marker, arcname="stock-csc-entry")
    stock = temp / "firmware.zip"
    with zipfile.ZipFile(stock, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for file in temp.glob("*.tar"):
            z.write(file, file.name + ".md5")
    extracted = samsung(stock, decoded, temp)
    assert extracted["size"] == info["size"] and extracted["slots"] == 2
    def modify(rom):
        for part in ("product", "system"):
            (rom / part / "marker").write_text("patched")
    edit(extracted, decoded, temp, modify)
    assert (decoded / "opaque.img").read_bytes() == opaque.read_bytes()
    rebuilt = temp / "rebuilt.img"
    repack(extracted, decoded, rebuilt, sparse=False)
    assert stock_layout(rebuilt) == extracted
    flash = temp / "flash"
    flash.mkdir()
    repack(extracted, decoded, flash / "super.img", sparse=True)
    (flash / "vbmeta.img").write_bytes(b"vbmeta fixture")
    filename, (ap, csc) = odin_files(stock, flash, temp)
    assert filename == "TEST_By.Yucca.zip"
    with tarfile.open(ap) as tar:
        assert set(tar.getnames()) == {"super.img.lz4", "vbmeta.img.lz4"}
        compressed = tar.extractfile("super.img.lz4").read()
        assert run("lz4", "-dc", input=compressed, capture_output=True).stdout == (flash / "super.img").read_bytes()
    with tarfile.open(csc) as tar:
        assert set(tar.getnames()) == {"stock-csc-entry", "TEST.pit"}
        assert tar.extractfile("TEST.pit").read() == b"stock"
    checked = temp / "checked"
    checked.mkdir()
    run("lpunpack", rebuilt, checked)
    mount = temp / "inspect"
    mount.mkdir()
    for part in ("product", "system"):
        run("mount", "-o", "loop,ro", checked / f"{part}.img", mount)
        try:
            p = mount / "marker"
            if part == "product":
                assert os.getxattr(mount, "security.selinux") == root_label
            assert p.read_text() == "patched"
            st = p.stat()
            assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (1000, 2000, 0o750)
            assert os.getxattr(p, "security.selinux") == b"u:object_r:system_file:s0\0"
        finally:
            run("umount", mount)
    stock_line = b"/dev/data /data f2fs nosuid wait,avb,quota,fileencryption=aes # keep\n"
    assert patch_fstab(stock_line, True, True) == b"/dev/data /data f2fs nosuid wait,quota # keep\n"
    assert patch_fstab(stock_line, False, False) == stock_line
    stock_mounts = (b"system /system ext4 ro,barrier=1,discard wait,logical,first_stage_mount,avb\n"
                    b"system /system erofs ro wait,logical,first_stage_mount,avb\n"
                    b"product /product erofs ro wait,logical,first_stage_mount\n" + stock_line)
    mixed = patch_fstab(stock_mounts, True, False, {"system": "f2fs"})
    assert mixed.count(b"system /system ") == 3
    assert b"system /system f2fs rw,discard wait,logical,first_stage_mount\n" in mixed
    assert b"product /product erofs ro " in mixed and b"fileencryption=aes" in mixed
    assert patch_fstab(mixed, True, False, {"system": "f2fs"}) == mixed
    check_ext4_growth(temp, tree)
    print("Passed Samsung sparse/raw super, EROFS, ext4, empty/opaque partitions and Android metadata")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(dir="/var/tmp") as temp:
        root = Path(temp)
        check(root)
        if "--f2fs" in sys.argv:
            image = root / "f2fs-test.img"
            run("truncate", "-s", "256M", image)
            run("mke2fs", "-q", "-t", "ext4", "-d", root / "tree", image)
            convert_to_f2fs(image)
            result = run("guestfish", "--ro", "--format=raw", "-a", image, "-m", "/dev/sda",
                         "cat", "/marker", ":", "getxattr", "/marker", "security.selinux",
                         capture_output=True)
            assert b"stock" in result.stdout and b"u:object_r:system_file:s0" in result.stdout
