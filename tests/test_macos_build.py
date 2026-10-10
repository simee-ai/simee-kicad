import pytest

from kicad_bundle.macos_build import dyld_name, missing_imports, parse_dyld_info, relink_plan, stale_build, targets, top_dir

IMPORTS = """/x/libkiapi.10.0.6.dylib [arm64]:
    -imports:
      __Unwind_Resume  (from libSystem)
      __ZN4absl12lts_2026010713hash_internal15MixingHashState5kSeedE  (from libabsl_hash.2601.0)
      __ZN6google8protobuf3Any5ClearEv  (from libprotobuf.35.1)
      _wxTheApp  (from <flat-namespace>)
      __ZN3fooC1Ev  (from libkicommon.10.0)
"""
EXPORTS = """/x/libprotobuf.35.1.0.dylib [arm64]:
    -exports:
        offset      symbol
        0x000419C0  __ZN6google8protobuf3Any5ClearEv
        0x0004159C  __ZN6google8protobuf3Any4SwapEPS1_
        0x00196B60  __ZN6google8protobuf8internal15ThreadSafeArena13thread_cache_E [per-thread]
        0x001174AC  __ZN6google8protobuf8internal16InternalMetadata7DoClearINS0_15UnknownFieldSetEEEvv [weak-def]
"""


def test_parse_dyld_info_reads_symbols_and_the_library_each_import_comes_from():
    assert parse_dyld_info(IMPORTS, "-imports") == [
        ("__Unwind_Resume", "libSystem"),
        ("__ZN4absl12lts_2026010713hash_internal15MixingHashState5kSeedE", "libabsl_hash.2601.0"),
        ("__ZN6google8protobuf3Any5ClearEv", "libprotobuf.35.1"),
        ("_wxTheApp", "<flat-namespace>"),
        ("__ZN3fooC1Ev", "libkicommon.10.0")]
    assert parse_dyld_info(EXPORTS, "-exports") == [
        ("__ZN6google8protobuf3Any5ClearEv", None), ("__ZN6google8protobuf3Any4SwapEPS1_", None),
        ("__ZN6google8protobuf8internal15ThreadSafeArena13thread_cache_E", None),  # flags don't hide an export
        ("__ZN6google8protobuf8internal16InternalMetadata7DoClearINS0_15UnknownFieldSetEEEvv", None)]


def test_dyld_name_is_how_dyld_info_names_a_library():
    assert dyld_name("/usr/lib/libSystem.B.dylib") == "libSystem"
    assert dyld_name("@rpath/libabsl_hash.2601.0.0.dylib") == "libabsl_hash.2601.0"
    assert dyld_name("@rpath/libnng.1.dylib") == "libnng"
    assert dyld_name("/System/Library/Frameworks/Cocoa.framework/Versions/A/Cocoa") == "Cocoa"
    assert dyld_name("@rpath/Versions/3.9/Python") == "Python"


def test_missing_imports_are_those_no_bundled_dependency_exports():
    imports = parse_dyld_info(IMPORTS, "-imports")
    system = {"libSystem"}
    exported = {"__ZN6google8protobuf3Any5ClearEv", "__ZN3fooC1Ev"}
    assert missing_imports(imports, system, exported) == [
        ("__ZN4absl12lts_2026010713hash_internal15MixingHashState5kSeedE", "libabsl_hash.2601.0")]


OFFICIAL = ["@rpath/libkicommon.10.0.6.dylib", "@rpath/libprotobuf.35.1.0.dylib", "@rpath/Versions/3.9/Python",
            "@rpath/../PlugIns/sim/libngspice.0.dylib", "/usr/lib/libc++.1.dylib"]


def test_relink_plan_gives_each_dependency_the_install_name_the_official_binary_uses():
    ours = ["/w/build/common/libkicommon.10.0.6.dylib", "/b/opt/protobuf/lib/libprotobuf.35.1.0.dylib",
            "/w/Python.framework/Versions/3.9/Python", "/w/sim/libngspice.0.dylib", "/usr/lib/libc++.1.dylib"]
    assert relink_plan(ours, OFFICIAL) == {
        "/w/build/common/libkicommon.10.0.6.dylib": "@rpath/libkicommon.10.0.6.dylib",
        "/b/opt/protobuf/lib/libprotobuf.35.1.0.dylib": "@rpath/libprotobuf.35.1.0.dylib",
        "/w/Python.framework/Versions/3.9/Python": "@rpath/Versions/3.9/Python",
        "/w/sim/libngspice.0.dylib": "@rpath/../PlugIns/sim/libngspice.0.dylib"}


def test_relink_plan_refuses_a_dependency_the_official_binary_does_not_have():
    with pytest.raises(RuntimeError, match="libnew.1.dylib"):
        relink_plan(["/b/opt/new/lib/libnew.1.dylib"], OFFICIAL)


def test_relink_plan_refuses_a_library_from_the_hosts_homebrew():
    with pytest.raises(RuntimeError, match="/usr/local/lib/libprotobuf.35.1.0.dylib"):
        relink_plan(["/usr/local/lib/libprotobuf.35.1.0.dylib"], OFFICIAL)


def test_targets_builds_the_executable_and_each_kiface_in_the_bundle():
    files = ["MacOS/kicad-cli", "PlugIns/_eeschema.kiface", "Frameworks/libkicommon.10.0.6.dylib",
             "PlugIns/_cvpcb.kiface"]
    assert targets(files) == ["cvpcb_kiface", "eeschema_kiface", "kicad-cli"]


def test_targets_builds_each_3d_plugin_in_the_bundle():
    files = ["PlugIns/3d/libs3d_plugin_oce.so", "PlugIns/3d/libs3d_plugin_vrml.so"]
    assert targets(files) == ["s3d_plugin_oce", "s3d_plugin_vrml"]


def test_top_dir_ignores_finders_files(tmp_path):
    (tmp_path / ".DS_Store").write_text("finder")
    (tmp_path / "wxWidgets-f9c61658f683").mkdir()
    assert top_dir(tmp_path) == tmp_path / "wxWidgets-f9c61658f683"


def test_a_build_dir_configured_for_another_source_tree_is_stale(tmp_path):
    build = tmp_path / "kicad-build"
    assert not stale_build(build, tmp_path / "src")
    build.mkdir()
    (build / "CMakeCache.txt").write_text(f"CMAKE_HOME_DIRECTORY:INTERNAL={tmp_path / 'old'}\n")
    assert stale_build(build, tmp_path / "src")
    assert not stale_build(build, tmp_path / "old")
