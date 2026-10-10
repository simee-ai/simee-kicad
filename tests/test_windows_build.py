import os
from pathlib import Path

import pytest

from fake_gitlab import branch
from kicad_bundle import windows_build
from tiny_pe import make_pe

THIRD_PARTY = ("wxbase332u_vc_x64_custom.dll", "libprotobuf.dll", "msvcp140.dll")


def test_vcpkg_commit_is_the_one_build_ps1_pinned_when_kicad_published_the_installer():
    build_ps1 = '$SentryDsn = ""\n)\n\n$vcpkgCommit = "66c0373dc7fca549e5803087b9487edfe3aca0a1";\n$cmakeVersion = "3.31.10"\n'

    history = branch("kicad/packaging/kicad-win-builder", "master",
                     [("kwb2", "2026-10-03T08:00:00.000+02:00"), ("kwb1", "2026-10-02T22:00:00.000Z")])

    def fetch(url: str) -> bytes:
        kwb = "https://gitlab.com/api/v4/projects/kicad%2Fpackaging%2Fkicad-win-builder/repository"
        if url in history:
            return history[url]
        if url == f"{kwb}/files/build.ps1/raw?ref=kwb1":
            return build_ps1.encode()
        raise AssertionError(url)

    assert windows_build.vcpkg_commit("2026-10-02T23:06:15Z", fetch) == "66c0373dc7fca549e5803087b9487edfe3aca0a1"


def _official(bin_dir: Path) -> None:
    for name in ("kicad-cli.exe", "_eeschema.dll", "kicommon.dll", "kigal.dll", "kiapi.dll", *THIRD_PARTY,
                 "plugins/3d/s3d_plugin_oce.dll"):
        make_pe(bin_dir / name)


def _built(built: Path, names=("kicad-cli.exe", "_eeschema.dll", "kicommon.dll", "kigal.dll", "kiapi.dll",
                               "kicad_3dsg.dll", "s3d_plugin_oce.dll"), linker=(14, 44)) -> None:
    for name in names:
        make_pe(built / name, linker)
        (built / name).write_bytes((built / name).read_bytes() + b"simee")


def test_cmake_args_are_kicad_win_builders_on_kicads_vcpkg_manifest(tmp_path):
    vcpkg = tmp_path / "vcpkg"
    args = windows_build.cmake_args(tmp_path / "src", tmp_path / "build", vcpkg)
    assert args[:4] == ["-G", "Ninja", "-S", str(tmp_path / "src")]
    assert f"-DCMAKE_TOOLCHAIN_FILE={vcpkg / 'scripts/buildsystems/vcpkg.cmake'}" in args
    assert "-DVCPKG_TARGET_TRIPLET=x64-windows" in args
    assert "-DVCPKG_INSTALL_OPTIONS=--clean-after-build" in args  # a hosted runner's disk can't hold every buildtree
    # kicad-win-builder's build.ps1 (translations off: the bundle has none; no Sentry: not KiCad's build)
    for flag in ("-DCMAKE_BUILD_TYPE=Release", "-DKICAD_BUILD_QA_TESTS=OFF", "-DKICAD_WIN32_DPI_AWARE=ON",
                 "-DKICAD_SCRIPTING_WXPYTHON=ON", "-DKICAD_BUILD_I18N=OFF"):
        assert flag in args
    assert not any("SENTRY" in a for a in args)


def test_toolset_is_the_msvc_version_that_linked_a_binary(tmp_path):
    assert windows_build.toolset(make_pe(tmp_path / "kicad-cli.exe", (14, 44))) == "14.44"


def test_parse_env_reads_set_output():
    text = "ALLUSERSPROFILE=C:\\ProgramData\r\nPath=C:\\VS\\bin;C:\\Windows\r\nVCToolsVersion=14.44.35207\r\n"
    assert windows_build.parse_env(text) == {"ALLUSERSPROFILE": "C:\\ProgramData", "Path": "C:\\VS\\bin;C:\\Windows",
                                             "VCToolsVersion": "14.44.35207"}


def test_overlay_replaces_every_kicad_file_and_leaves_third_party_ones(tmp_path):
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    _built(built)
    replaced = windows_build.overlay(bin_dir, built)
    assert replaced == ["_eeschema.dll", "kiapi.dll", "kicad-cli.exe", "kicommon.dll", "kigal.dll",
                        "plugins/3d/s3d_plugin_oce.dll"]
    for name in replaced:
        assert (bin_dir / name).read_bytes().endswith(b"simee")
    for name in THIRD_PARTY:
        assert not (bin_dir / name).read_bytes().endswith(b"simee")
    assert not (bin_dir / "kicad_3dsg.dll").exists()  # not in the official closure, so not needed


def test_overlay_refuses_to_leave_an_official_kicad_file(tmp_path):
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    _built(built, names=("kicad-cli.exe", "_eeschema.dll", "kicommon.dll", "kiapi.dll"))
    with pytest.raises(RuntimeError, match="kigal.dll"):
        windows_build.overlay(bin_dir, built)


def test_overlay_refuses_to_leave_an_official_3d_plugin(tmp_path):
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    _built(built, names=("kicad-cli.exe", "_eeschema.dll", "kicommon.dll", "kigal.dll", "kiapi.dll"))
    with pytest.raises(RuntimeError, match="s3d_plugin_oce.dll"):
        windows_build.overlay(bin_dir, built)


def test_overlay_refuses_another_msvc_than_the_official_build(tmp_path):
    # The bundle keeps KiCad's C++ runtime, which must be at least as new as the compiler, and the
    # official third-party DLLs, which our KiCad files link against.
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    _built(built, linker=(14, 50))
    with pytest.raises(RuntimeError, match="14.50.*14.44"):
        windows_build.overlay(bin_dir, built)


def test_overlay_refuses_a_dll_the_official_build_did_not_need(tmp_path, monkeypatch):
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    _built(built)
    official = {"kicommon.dll": ["kiapi.dll", "libprotobuf.dll", "KERNEL32.dll"]}
    simee = {"kicommon.dll": ["kiapi.dll", "LIBPROTOBUF.dll", "KERNEL32.dll", "USER32.dll", "sentry.dll",
                              "api-ms-win-core-winrt-error-l1-1-1.dll"]}

    def deps(path: Path) -> list[str]:
        return (simee if path.parent == built else official).get(path.name, ["KERNEL32.dll"])

    monkeypatch.setattr(windows_build.pe, "deps", deps)
    with pytest.raises(RuntimeError, match=r"kicommon.dll.*sentry.dll") as err:
        windows_build.overlay(bin_dir, built)
    assert "USER32" in str(err.value)  # new imports are refused whether or not Windows has them...
    assert "api-ms-win" not in str(err.value)  # ...but API sets, which Windows resolves itself (SDK dependent)


def test_overlay_accepts_a_dll_another_official_file_imports(tmp_path, monkeypatch):
    # The official _cvpcb.dll reaches Windows' kernel only through API sets, ours through KERNEL32.dll,
    # which the official _eeschema.dll imports too: the bundle already relies on it (simee-kicad#7).
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    make_pe(bin_dir / "_cvpcb.dll")
    _built(built, names=("kicad-cli.exe", "_eeschema.dll", "_cvpcb.dll", "kicommon.dll", "kigal.dll", "kiapi.dll",
                         "s3d_plugin_oce.dll"))
    official = {"_cvpcb.dll": ["api-ms-win-core-file-l1-1-0.dll", "kicommon.dll"],
                "_eeschema.dll": ["kernel32.dll", "kicommon.dll"]}
    simee = {"_cvpcb.dll": ["KERNEL32.dll", "kicommon.dll"]}

    def deps(path: Path) -> list[str]:
        return (simee if path.parent == built else official).get(path.name, [])

    monkeypatch.setattr(windows_build.pe, "deps", deps)
    assert "_cvpcb.dll" in windows_build.overlay(bin_dir, built)


def test_overlay_accepts_new_windows_api_sets(tmp_path, monkeypatch):
    bin_dir, built = tmp_path / "bundle/bin", tmp_path / "built"
    _official(bin_dir)
    _built(built)
    monkeypatch.setattr(windows_build.pe, "deps", lambda path: ["KERNEL32.dll"] + (
        ["api-ms-win-core-heap-l2-1-0.dll"] if path.parent == built else []))
    assert "kicommon.dll" in windows_build.overlay(bin_dir, built)


def test_built_files_are_found_once_each_outside_vcpkgs_tree(tmp_path):
    build = tmp_path / "build"
    make_pe(build / "kicad/kicad-cli.exe")
    make_pe(build / "eeschema/_eeschema.dll")
    make_pe(build / "common/kicommon.dll")
    make_pe(build / "plugins/3d/oce/s3d_plugin_oce.dll")
    make_pe(build / "vcpkg_installed/x64-windows/bin/kicommon.dll")
    found = windows_build.collect_built(build, tmp_path / "out")
    assert sorted(p.name for p in found.iterdir()) == ["_eeschema.dll", "kicad-cli.exe", "kicommon.dll",
                                                       "s3d_plugin_oce.dll"]
    make_pe(build / "other/kicommon.dll")
    with pytest.raises(RuntimeError, match="kicommon.dll"):
        windows_build.collect_built(build, tmp_path / "out")


@pytest.mark.skipif(os.name == "nt", reason="the build itself runs on Windows")
def test_build_needs_windows(tmp_path):
    with pytest.raises(RuntimeError, match="Windows"):
        windows_build.build(tmp_path / "src.tar.gz", tmp_path, tmp_path / "cache", "14.44", "66c0373")


def test_swig_is_kicad_win_builders_swigwin_checked_and_unpacked(tmp_path):
    import hashlib, io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("swigwin-4.3.1/swig.exe", b"swig")
        z.writestr("swigwin-4.3.1/Lib/swig.swg", b"lib")
    data = buf.getvalue()
    assert windows_build.SWIG_URL.endswith("swigwin-4.3.1.zip/download?use_mirror=pilotfiber")
    folder = windows_build.swig(tmp_path, fetch=lambda url: data, digest=hashlib.sha256(data).hexdigest())
    assert (folder / "swig.exe").read_bytes() == b"swig" and (folder / "Lib/swig.swg").exists()
    with pytest.raises(RuntimeError, match="doesn't match"):
        windows_build.swig(tmp_path / "again", fetch=lambda url: data + b"x", digest=hashlib.sha256(data).hexdigest())


def test_unpack_strips_the_top_folder_and_skips_links_out_of_the_tree(tmp_path):
    import io, tarfile
    src = tmp_path / "src.tar.gz"
    with tarfile.open(src, "w:gz") as tar:
        data = b"cmake_minimum_required()"
        info = tarfile.TarInfo("simee-kicad-4e18395/CMakeLists.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for name, target in (("qa/tests/resources", "/usr/share/kicad"), ("qa/escape", "../../../etc")):
            link = tarfile.TarInfo(f"simee-kicad-4e18395/{name}")
            link.type, link.linkname = tarfile.SYMTYPE, target
            tar.addfile(link)
    windows_build._unpack(src, tmp_path / "tree")
    assert (tmp_path / "tree/CMakeLists.txt").read_bytes() == data
    assert not (tmp_path / "tree/qa/tests/resources").exists() and not (tmp_path / "tree/qa/escape").is_symlink()
