from pathlib import Path

import pytest

import tiny_macho
from archives import targz
from kicad_bundle import homebrew, macbuilder, macos_third_party
from kicad_bundle.homebrew import Bottle
from kicad_bundle.macbuilder import GitSource, Pins

GLIB_ARM, GLIB_INTEL = "7AD804A6-C91B-3A8F-81DB-2E0726D3D42E", "00000000-0000-0000-0000-000000000001"
UNTIL = "2026-08-29T15:43:28Z"


def _app(contents: Path, extra: str | None = None) -> list[Path]:
    files = {"MacOS/kicad-cli": b"cli", "PlugIns/_eeschema.kiface": b"kiface",
             "Frameworks/libkicommon.10.0.6.dylib": b"ki", "PlugIns/3d/libs3d_plugin_oce.so": b"plugin",
             "Frameworks/libglib-2.0.0.dylib": tiny_macho.fat(("arm64", tiny_macho.thin("arm64", GLIB_ARM)),
                                                              ("x86_64", tiny_macho.thin("x86_64", GLIB_INTEL))),
             "Frameworks/libwx_osx_cocoau-3.2.0.4.1.dylib": "wxWidgets 3.2.8".encode("utf-32-le"),
             "PlugIns/sim/libngspice.0.dylib": b"\x0045.2\x00",
             "Frameworks/Python.framework/Versions/3.9/Python": b"3.9.13 (main"}
    if extra:
        files[extra] = b"?"
    for rel, data in files.items():
        (contents / rel).parent.mkdir(parents=True, exist_ok=True)
        (contents / rel).write_bytes(data)
    return [contents / rel for rel in files]


@pytest.fixture
def builders(tmp_path, monkeypatch):
    """Homebrew and kicad-mac-builder answered locally: glib's bottles, the 10.0 pins, sources."""
    glib_src = targz(tmp_path / "src/glib-2.88.3.tar.xz", {"glib-2.88.3/COPYING": b"LGPL"})
    wx_src = targz(tmp_path / "src/wx.tar.gz", {"wxWidgets-f9c61658f683/docs/licence.txt": b"wxWindows",
                                                 "wxWidgets-f9c61658f683/include/wx/version.h":
                                                     b"#define wxMAJOR_VERSION      3\n#define wxMINOR_VERSION      2\n"
                                                     b"#define wxRELEASE_NUMBER     8\n"})
    ngspice_src = targz(tmp_path / "src/ngspice.tar.gz", {"ngspice-aaaaaaaaaaaa/COPYING": b"BSD"})
    python_src = targz(tmp_path / "src/Python-3.9.13.tar.xz", {"Python-3.9.13/LICENSE": b"PSF"})
    calls = []

    def match(name, arch, libs, until, cache, fetch):
        assert (name, until, set(libs)) == ("glib", UNTIL, {"libglib-2.0.0.dylib"})
        calls.append((arch, libs["libglib-2.0.0.dylib"].uuid))
        tag = "arm64_sonoma" if arch == "arm64" else "sonoma"
        return Bottle("glib", "2.88.3", tag, tag[:1] * 64, "c0ffee0123456789", tmp_path / "info")

    def git_archive(source, sha, name, cache):
        return {"wxWidgets": wx_src, "ngspice": ngspice_src}[name]

    monkeypatch.setattr(homebrew, "match", match)
    monkeypatch.setattr(homebrew, "sources", lambda bottle, cache, fetch: [
        ("glib-2.88.3.tar.xz", glib_src), ("glib.rb", tmp_path / "info/formula.rb")])
    monkeypatch.setattr(macbuilder, "pins", lambda version, until, fetch: Pins(
        "b0b0b0b0b0b0", GitSource("https://gitlab.com/kicad/code/wxWidgets.git", "kicad/macos-wx-3.2"),
        GitSource("https://git.code.sf.net/p/ngspice/ngspice", "ngspice-45.2"), "3.9.13"))
    monkeypatch.setattr(macbuilder, "commit", lambda source, until, fetch: {
        "kicad/macos-wx-3.2": "f9c61658f6831b9fd5e0427531ab4e735bf9a24b", "ngspice-45.2": "a" * 40}[source.ref])
    monkeypatch.setattr(macbuilder, "git_archive", git_archive)
    monkeypatch.setattr(macos_third_party, "python_source", lambda version, cache, fetch: python_src)
    return calls


def test_collect_finds_every_librarys_component_per_architecture(tmp_path, builders):
    contents = tmp_path / "KiCad.app/Contents"
    third = macos_third_party.collect(contents, _app(contents), "10.0.6", UNTIL, tmp_path / "cache", fetch=None)
    assert sorted(builders) == [("arm64", GLIB_ARM), ("x86_64", GLIB_INTEL)]
    assert [(c.name, c.version) for c in third.components] == [
        ("Python", "3.9.13"), ("glib", "2.88.3"), ("ngspice", "45.2"), ("wxWidgets", "3.2.8")]
    assert third.rows["arm64"] == [
        ("Frameworks/Python.framework/Versions/3.9/Python", "Python 3.9.13",
         "python.org, made relocatable by kicad-mac-builder b0b0b0b0b0"),
        ("Frameworks/libglib-2.0.0.dylib", "glib 2.88.3", "Homebrew arm64_sonoma bottle, homebrew-core c0ffee0123"),
        ("Frameworks/libwx_osx_cocoau-3.2.0.4.1.dylib", "wxWidgets 3.2.8",
         "https://gitlab.com/kicad/code/wxWidgets.git kicad/macos-wx-3.2 at f9c61658f6 (kicad-mac-builder b0b0b0b0b0)"),
        ("PlugIns/sim/libngspice.0.dylib", "ngspice 45.2",
         f"https://git.code.sf.net/p/ngspice/ngspice ngspice-45.2 at {'a' * 10} (kicad-mac-builder b0b0b0b0b0)")]
    assert third.rows["x86_64"][1] == ("Frameworks/libglib-2.0.0.dylib", "glib 2.88.3",
                                       "Homebrew sonoma bottle, homebrew-core c0ffee0123")
    # what a build from source pours to compile against the same libraries (macos_build.py)
    assert {arch: [(b.formula, b.tag) for b in bottles] for arch, bottles in third.bottles.items()} == {
        "arm64": [("glib", "arm64_sonoma")], "x86_64": [("glib", "sonoma")]}


def test_a_library_homebrew_built_from_source_names_the_bottle_standing_in_for_it(tmp_path, builders, monkeypatch):
    def match(name, arch, libs, until, cache, fetch):
        if arch == "arm64":
            return Bottle("glib", "2.88.3", "arm64_sonoma", "a" * 64, "c0ffee0123456789", tmp_path / "info")
        return Bottle("glib", "2.88.3", "arm64_sonoma", "a" * 64, "c0ffee0123456789", tmp_path / "info",
                      built_for="sonoma")

    monkeypatch.setattr(homebrew, "match", match)
    contents = tmp_path / "full/KiCad.app/Contents"
    third = macos_third_party.collect(contents, _app(contents), "10.0.6", UNTIL, tmp_path / "cache", fetch=None)
    assert [(c.name, c.version) for c in third.components if c.name == "glib"] == [("glib", "2.88.3")]
    assert third.rows["x86_64"][1] == ("Frameworks/libglib-2.0.0.dylib", "glib 2.88.3",
                                       "Homebrew sonoma keg built from source, formula and source as in its "
                                       "arm64_sonoma bottle, homebrew-core c0ffee0123")
    root = tmp_path / "kicad-cli-10.0.6-macos-x86_64"
    macos_third_party.write_notices(third, root, "x86_64", "10.0.6", "kicad-cli-10.0.6-macos-sources.tar")
    assert "built from source" in (root / "THIRD-PARTY.txt").read_text().split("file\t")[0]


def test_collect_fails_on_a_library_of_unknown_provenance(tmp_path, builders):
    contents = tmp_path / "KiCad.app/Contents"
    with pytest.raises(RuntimeError, match="libmystery.1.dylib"):
        macos_third_party.collect(contents, _app(contents, "Frameworks/libmystery.1.dylib"), "10.0.6", UNTIL,
                                  tmp_path / "cache", fetch=None)


def test_collect_fails_when_a_built_component_is_not_the_pinned_version(tmp_path, builders):
    contents = tmp_path / "KiCad.app/Contents"
    files = _app(contents)
    (contents / "PlugIns/sim/libngspice.0.dylib").write_bytes(b"\x0044\x00")
    with pytest.raises(RuntimeError, match="ngspice 45.2"):
        macos_third_party.collect(contents, files, "10.0.6", UNTIL, tmp_path / "cache", fetch=None)


def test_write_notices_lists_each_file_and_ships_each_components_licences(tmp_path, builders):
    contents = tmp_path / "full/KiCad.app/Contents"
    third = macos_third_party.collect(contents, _app(contents), "10.0.6", UNTIL, tmp_path / "cache", fetch=None)
    root = tmp_path / "kicad-cli-10.0.6-macos-arm64"
    macos_third_party.write_notices(third, root, "arm64", "10.0.6", "kicad-cli-10.0.6-macos-sources.tar")
    text = (root / "THIRD-PARTY.txt").read_text()
    assert "kicad-cli-10.0.6-macos-sources.tar" in text
    assert "KiCad.app/Contents/Frameworks/libglib-2.0.0.dylib\tglib 2.88.3\tHomebrew arm64_sonoma bottle" in text
    assert "Independent JPEG Group" in text  # wxWidgets' built-in libjpeg asks for this credit
    licences = root / "KiCad.app/Contents/Resources/Licenses"
    assert (licences / "glib/COPYING").read_bytes() == b"LGPL"
    assert (licences / "wxWidgets/docs/licence.txt").read_bytes() == b"wxWindows"
    assert (licences / "Python/LICENSE").read_bytes() == b"PSF"


def test_notices_of_a_bundle_built_from_a_simee_branch_say_kicad_is_modified(tmp_path, builders):
    contents = tmp_path / "full/KiCad.app/Contents"
    third = macos_third_party.collect(contents, _app(contents), "10.0.6", UNTIL, tmp_path / "cache", fetch=None)
    root = tmp_path / "kicad-cli-10.0.6-macos-arm64"
    macos_third_party.write_notices(third, root, "arm64", "10.0.6", "kicad-cli-10.0.6-macos-sources.tar",
                                    simee_sha="4e183959768bae7846a66b52135087adf24b82d4")
    text = (root / "THIRD-PARTY.txt").read_text()
    assert "unmodified" not in text.split("\n\n")[0]
    assert "simee-kicad commit 4e183959768bae7846a66b52135087adf24b82d4" in text
    assert "modified" in text
