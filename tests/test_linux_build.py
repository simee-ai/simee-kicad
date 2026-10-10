from datetime import datetime, timezone
from pathlib import Path

import pytest

from kicad_bundle import linux_build
from kicad_bundle.elf import ARCHES

DIGEST = "kicad/kicad@sha256:" + "ab" * 32


def test_snapshot_stamp_is_utc_to_the_second():
    when = datetime(2026, 9, 22, 12, 55, 49, 700000, tzinfo=timezone.utc).timestamp()
    assert linux_build.snapshot_stamp(when) == "20260922T125549Z"


def test_dockerfile_builds_on_the_official_image_with_its_packages_held():
    text = linux_build.dockerfile(DIGEST, "20260922T125549Z")
    lines = text.splitlines()
    assert lines[0] == f"FROM {DIGEST}"
    # Debian as it was when the image was made, security updates included...
    for suite in ("debian/20260922T125549Z/ trixie main", "debian/20260922T125549Z/ trixie-updates main",
                  "debian-security/20260922T125549Z/ trixie-security main"):
        assert f"https://snapshot.debian.org/archive/{suite}'" in text
    # ...and no installed package may change, so the -dev packages match the image's libraries exactly.
    hold = next(i for i, l in enumerate(lines) if "apt-mark hold" in l)
    install = next(i for i, l in enumerate(lines) if "apt-get install" in l)
    assert hold < install
    for dev in ("libwxgtk3.2-dev", "libprotobuf-dev", "libgit2-dev", "libocct-foundation-dev", "libngspice0-dev"):
        assert dev in text
    # KiCad's own cmake options for the official image (kicad-docker Dockerfile.10.0-stable).
    for flag in ("-DKICAD_SCRIPTING_WXPYTHON=ON", "-DKICAD_USE_OCC=ON", "-DKICAD_SPICE=ON",
                 "-DKICAD_USE_CMAKE_FINDPROTOBUF=ON", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_PREFIX=/usr"):
        assert flag in text
    assert ("ninja kicad-cli eeschema_kiface cvpcb_kiface pcbnew_kiface s3d_plugin_idf s3d_plugin_oce "
            "s3d_plugin_vrml") in text
    for name in ("_cvpcb.kiface", "_pcbnew.kiface", "libs3d_plugin_oce.so"):  # copied out with the rest
        assert f"-name {name}" in text


# KiCad's own files a build makes besides its libraries, and where the bundle has them.
OWN = ("kicad-cli", "_eeschema.kiface", "_cvpcb.kiface", "_pcbnew.kiface", "libs3d_plugin_idf.so",
       "libs3d_plugin_oce.so", "libs3d_plugin_vrml.so")
OWN_PLACED = ["libexec/_cvpcb.kiface", "libexec/_eeschema.kiface", "libexec/_pcbnew.kiface", "libexec/kicad-cli",
              "libexec/plugins/3d/libs3d_plugin_idf.so", "libexec/plugins/3d/libs3d_plugin_oce.so",
              "libexec/plugins/3d/libs3d_plugin_vrml.so"]


def _bundle(root: Path, libs: list[str]) -> None:
    for rel in (*OWN_PLACED, *(f"lib/{l}" for l in libs), "lib/libwx.so.0"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("official")


def test_overlay_replaces_kicads_own_files_only(tmp_path):
    root, built = tmp_path / "bundle", tmp_path / "built"
    _bundle(root, ["libkicommon.so.10.0.6", "libkigal.so.10.0.6"])
    built.mkdir()
    for name in (*OWN, "libkicommon.so.10.0.6", "libkigal.so.10.0.6",
                 "libkiapi.so.10.0.6"):
        (built / name).write_text("simee")
    replaced = linux_build.overlay(root, built)
    assert sorted(replaced) == ["lib/libkicommon.so.10.0.6", "lib/libkigal.so.10.0.6", *OWN_PLACED]
    for rel in replaced:
        assert (root / rel).read_text() == "simee"
    assert (root / "lib/libwx.so.0").read_text() == "official"
    assert not (root / "lib/libkiapi.so.10.0.6").exists()  # not in the official closure, so not needed


def test_overlay_refuses_to_leave_an_official_kicad_file(tmp_path):
    root, built = tmp_path / "bundle", tmp_path / "built"
    _bundle(root, ["libkicommon.so.10.0.6", "libkigal.so.10.0.6"])
    built.mkdir()
    for name in (*OWN, "libkicommon.so.10.0.6"):
        (built / name).write_text("simee")
    with pytest.raises(RuntimeError, match="libkigal.so.10.0.6"):
        linux_build.overlay(root, built)


def test_overlay_of_another_version_swaps_kicads_libraries_for_its_own(tmp_path):
    # a release-candidate rehearsal builds 10.0.7 on the 10.0.6 image: KiCad's libraries carry their version
    root, built = tmp_path / "bundle", tmp_path / "built"
    _bundle(root, ["libkicommon.so.10.0.6", "libkigal.so.10.0.6"])
    built.mkdir()
    for name in (*OWN, "libkicommon.so.10.0.7", "libkigal.so.10.0.7",
                 "libkiapi.so.10.0.7"):
        (built / name).write_text("simee")
    replaced = linux_build.overlay(root, built)
    assert sorted(replaced) == ["lib/libkicommon.so.10.0.7", "lib/libkigal.so.10.0.7", *OWN_PLACED]
    assert sorted(p.name for p in (root / "lib").glob("libki*")) == ["libkicommon.so.10.0.7", "libkigal.so.10.0.7"]


def test_source_versions_must_match_the_image():
    image = {"libwxgtk3.2-1t64": ("wxwidgets3.2", "3.2.8+dfsg-2"), "libgit2-1.9": ("libgit2", "1.9.0+ds-2")}
    same = {"libwxgtk3.2-dev": ("wxwidgets3.2", "3.2.8+dfsg-2"), "cmake": ("cmake", "3.31.6-2")}
    assert linux_build.mismatched_sources(image, same) == []
    newer = {"libgit2-dev": ("libgit2", "1.9.0+ds-2+deb13u1")}
    assert linux_build.mismatched_sources(image, newer) == [("libgit2", "1.9.0+ds-2", "1.9.0+ds-2+deb13u1")]


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_cli_passes_simee_ref_to_the_packager(monkeypatch, tmp_path, platform):
    from kicad_bundle import cli
    seen = {}
    monkeypatch.setitem(cli.PACKAGERS, platform, lambda *a, **k: seen.update(k) or [])
    cli.main(["--kicad-version", "10.0.6", "--platform", platform, "--simee-ref", "simee/10.0.6", "--cache", str(tmp_path)])
    assert seen == {"simee_ref": "simee/10.0.6"}


def test_cli_passes_a_base_image_to_the_linux_packager(monkeypatch, tmp_path):
    from kicad_bundle import cli
    seen = {}
    monkeypatch.setitem(cli.PACKAGERS, "linux", lambda *a, **k: seen.update(k) or [])
    cli.main(["--kicad-version", "11.0.0-rc1", "--platform", "linux", "--simee-ref", "rehearsal/11.0.0-rc1",
              "--base-image", "kicad/kicad:10.0.6", "--cache", str(tmp_path)])
    assert seen == {"simee_ref": "rehearsal/11.0.0-rc1", "base_image": "kicad/kicad:10.0.6"}


@pytest.mark.parametrize("platform", ["macos", "windows"])
def test_cli_refuses_a_base_image_outside_linux(monkeypatch, tmp_path, platform):
    from kicad_bundle import cli
    monkeypatch.setitem(cli.PACKAGERS, platform, lambda *a, **k: pytest.fail("packaged"))
    with pytest.raises(SystemExit):
        cli.main(["--kicad-version", "11.0.0-rc1", "--platform", platform, "--simee-ref", "x",
                  "--base-image", "kicad/kicad:10.0.6", "--cache", str(tmp_path)])


# The official image's history ends in the Debian image it was built FROM (debuerreotype's command).
HISTORY = ["USER kicad", "COPY /usr/installtemp/bin /usr/bin # buildkit",
           "ARG USER_NAME=kicad", "# debian.sh --arch 'amd64' out/ 'trixie' '@1789689600'"]


def test_debian_base_is_the_dated_debian_image_the_official_one_was_built_from():
    assert linux_build.debian_base(HISTORY) == "debian:trixie-20260918"
    assert linux_build.debian_base(["# debian.sh --arch 'arm64' --slim out/ 'trixie' '@1789689600'"]) == \
        "debian:trixie-20260918-slim"
    with pytest.raises(RuntimeError, match="Debian"):
        linux_build.debian_base(["FROM ubuntu"])


def test_runtime_dockerfile_installs_the_official_images_packages_from_debians_archive_of_that_day():
    text = linux_build.runtime_dockerfile("debian:trixie-20260918", "20260922T125549Z", ["zlib1g", "libgit2-1.9"])
    lines = text.splitlines()
    assert lines[0] == "FROM debian:trixie-20260918"
    for suite in ("debian/20260922T125549Z/ trixie main", "debian/20260922T125549Z/ trixie-updates main",
                  "debian-security/20260922T125549Z/ trixie-security main"):
        # http: the Debian image has no CA certificates yet (apt checks the archive's signatures)
        assert f"http://snapshot.debian.org/archive/{suite}'" in text
    # each package Debian has for the architecture (the official image has some amd64-only ones)
    assert "printf '%s\\n' libgit2-1.9 zlib1g | sort > /tmp/wanted" in text
    assert "apt-cache dumpavail" in text
    assert "apt-get install -y --no-install-recommends $(comm -12 /tmp/wanted /tmp/available)" in text


def test_native_context_lays_kicads_files_out_where_the_official_image_has_them(tmp_path):
    built, rootfs, context = tmp_path / "built", tmp_path / "rootfs", tmp_path / "context"
    built.mkdir()
    for name in (*OWN, "libkicommon.so.10.0.6", "dpkg-sources.txt"):
        (built / name).write_text(name)
    (rootfs / "usr/share/kicad/schemas").mkdir(parents=True)
    (rootfs / "usr/share/kicad/schemas/api.v1.schema.json").write_text("{}")
    linux_build.native_context(context, "simee-kicad-linux-arm64-runtime", built, rootfs,
                               ARCHES["arm64"], data=("usr/share/kicad/schemas",))
    found = sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file())
    assert found == ["Dockerfile", "usr/bin/_cvpcb.kiface", "usr/bin/_eeschema.kiface", "usr/bin/_pcbnew.kiface",
                     "usr/bin/kicad-cli", "usr/lib/aarch64-linux-gnu/kicad/plugins/3d/libs3d_plugin_idf.so",
                     "usr/lib/aarch64-linux-gnu/kicad/plugins/3d/libs3d_plugin_oce.so",
                     "usr/lib/aarch64-linux-gnu/kicad/plugins/3d/libs3d_plugin_vrml.so",
                     "usr/lib/aarch64-linux-gnu/libkicommon.so.10.0.6",
                     "usr/share/kicad/schemas/api.v1.schema.json"]
    assert (context / "Dockerfile").read_text().splitlines() == ["FROM simee-kicad-linux-arm64-runtime", "COPY usr/ /usr/"]


def test_cli_passes_an_arch_to_the_linux_packager(monkeypatch, tmp_path):
    from kicad_bundle import cli
    seen = {}
    monkeypatch.setitem(cli.PACKAGERS, "linux", lambda *a, **k: seen.update(k) or [])
    cli.main(["--kicad-version", "10.0.6", "--platform", "linux", "--arch", "arm64", "--simee-ref", "simee/10.0.6",
              "--cache", str(tmp_path)])
    assert seen == {"simee_ref": "simee/10.0.6", "arch": "arm64"}


@pytest.mark.parametrize("platform", ["macos", "windows"])
def test_cli_refuses_an_arch_outside_linux(monkeypatch, tmp_path, platform):
    from kicad_bundle import cli
    monkeypatch.setitem(cli.PACKAGERS, platform, lambda *a, **k: pytest.fail("packaged"))
    with pytest.raises(SystemExit):
        cli.main(["--kicad-version", "10.0.6", "--platform", platform, "--arch", "arm64", "--simee-ref", "x",
                  "--cache", str(tmp_path)])
