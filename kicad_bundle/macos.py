"""macOS: official universal DMG -> one trimmed KiCad.app per architecture, each with the licences of
its third-party libraries, and one archive of their sources for both (see macos_third_party). Given a
simee-kicad branch, KiCad's own files in it are rebuilt from that branch (see macos_build)."""

import platform
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from kicad_bundle import (imports, macbuilder, macho, macos_build, macos_third_party, release, simee_source, smoke,
                          third_party)
from kicad_bundle.bundle import KIFACES, PLUGINS_3D, archive, copy_tree, prune, remove
from kicad_bundle.closure import closure
from kicad_bundle.fetch import fetch_url

ARCHES = ("arm64", "x86_64")
# kicad-cli, the kifaces and the 3D plugins it loads; they link the rest.
ROOTS = ("MacOS/kicad-cli", *(f"PlugIns/_{k}.kiface" for k in KIFACES),
         *(f"PlugIns/3d/libs3d_plugin_{p}.so" for p in PLUGINS_3D))
TOPS = ("MacOS", "Frameworks", "PlugIns")
DROP = ("SharedSupport", "Applications", "_CodeSignature")  # libraries, sub-apps, stale signature


@contextmanager
def mounted(dmg: Path):
    with tempfile.TemporaryDirectory() as mnt:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mnt, str(dmg)],
                       check=True, capture_output=True)
        try:
            yield Path(mnt)
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", mnt], check=False)


def _cli_command(cli: Path, arch: str) -> list[str] | None:
    host = platform.machine()
    if arch == host:
        return [str(cli)]
    if host == "arm64" and arch == "x86_64":
        return ["arch", "-x86_64", str(cli)]  # needs Rosetta
    return None


def _overlay(app_contents: Path, built: dict[str, Path], arch: str) -> None:
    """Replace KiCad's own files (thinned official ones) with the built ones, linked like them."""
    for rel, path in built.items():
        dest = app_contents / rel
        official = dest.with_name(dest.name + ".official")
        dest.replace(official)
        shutil.copy2(path, dest)
        macho.thin(dest, arch)
        macos_build.relink(dest, official)
        official.unlink()
        macho.adhoc_sign(dest)
    macos_build.check_imports(app_contents, [app_contents / rel for rel in built], arch)


def _smoke(cli: list[str] | None, arch: str, simee_built: bool) -> None:
    if cli is None:
        print(f"  smoke test skipped: can't run {arch} on {platform.machine()}")
        return
    smoke.check(cli)
    if simee_built:  # `sch import` and the Eagle board fix are simee's: only a bundle built from a simee branch has them
        imports.check(cli)
    print(f"  smoke test passed ({arch}{', sch and pcb import of every fixture' if simee_built else ''})")


def package(version: str, out_dir: Path, cache: Path, work: Path, run_smoke: bool = True,
            simee_ref: str | None = None) -> list[Path]:
    """The bundles and their third-party sources. With simee_ref (a simee-kicad branch such as
    simee/10.0.6), KiCad's own files are built from it (macos_build), and its source tarball is one
    of the results."""
    sha = simee_source.resolve_ref(simee_ref) if simee_ref else None
    if sha:
        print(f"  building KiCad's own files from simee-kicad {sha} ({simee_ref})")
        kicad_source = simee_source.source_archive(sha, out_dir / f"kicad-{version}-source.tar.gz")
        src = macos_build.unpack(kicad_source, work / "kicad-src")
    found = release.installer(version, "kicad-unified-universal-*.dmg",
                              f"osx/stable/kicad-unified-universal-{version}.dmg")
    dmg = release.cached(found, version, cache)
    full = work / "full" / "KiCad.app"
    if full.exists():
        remove(full.parent)
    with mounted(dmg) as mnt:
        app = next(mnt.glob("*/KiCad.app"), None) or next(mnt.glob("KiCad.app"))
        copy_tree(app, full, exclude=tuple(f"Contents/{d}" for d in DROP))
    contents = full / "Contents"

    missing: list[tuple[str, str]] = []
    keep = closure([contents / r for r in ROOTS], deps=macho.deps, resolve=macho.make_resolver(contents),
                   missing=missing)
    if missing:
        raise RuntimeError(f"unresolved libraries: {missing}")
    third = macos_third_party.collect(contents.resolve(), sorted({k.resolve() for k in keep}), version,
                                      found.published_at, cache, archs=ARCHES)
    sources = f"kicad-cli-{version}-macos-sources.tar"
    own = sorted(str(k.relative_to(contents.resolve())) for k in {k.resolve() for k in keep}
                 if macos_third_party.KICAD.fullmatch(k.name))
    if sha:
        until = found.published_at
        pins = macbuilder.pins(version, until, fetch_url)

    built = []
    for arch in ARCHES:
        root = work / f"kicad-cli-{version}-macos-{arch}"
        if root.exists():
            remove(root)
        copy_tree(full, root / "KiCad.app")
        app_contents = root / "KiCad.app" / "Contents"
        prune([app_contents / t for t in TOPS], {app_contents / k.relative_to(contents) for k in keep})
        for f in sorted(app_contents.rglob("*")):
            if f.is_file() and not f.is_symlink() and macho.is_macho(f):
                macho.thin(f, arch)
                macho.adhoc_sign(f)
        if sha:
            minos = macho.slices((contents / "MacOS" / "kicad-cli").read_bytes())[arch].minos
            official = macos_build.Official(contents, f"{minos[0]}.{minos[1]}", pins, until)
            _overlay(app_contents, macos_build.build_kicad(src, own, arch, official, third.bottles[arch], cache,
                                                           fetch_url, work / f"build-{arch}"), arch)
        macos_third_party.write_notices(third, root, arch, version, sources, simee_sha=sha)
        if run_smoke:
            _smoke(_cli_command(app_contents / "MacOS" / "kicad-cli", arch), arch, simee_built=bool(sha))
        built.append(archive(root, out_dir, "tar.gz"))
    return [*built, third_party.sources_archive(third.components, out_dir / sources), *([kicad_source] if sha else [])]
