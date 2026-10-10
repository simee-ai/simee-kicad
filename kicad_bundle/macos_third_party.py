"""Licences and sources of the third-party libraries in the macOS bundle. Each library comes either
from a Homebrew bottle (found by Mach-O UUID, see homebrew.py) or from what kicad-mac-builder builds
itself (see macbuilder.py); a library that is neither, nor KiCad's own, fails the build."""

import re
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from kicad_bundle import homebrew, macbuilder, macho, third_party
from kicad_bundle.cache import HOMEBREW_BOTTLES, MACOS_SOURCES
from kicad_bundle.fetch import Fetch, fetch_url, write_atomically
from kicad_bundle.third_party import Component, licences

# KiCad's own files: its source (a separate release asset) covers them.
KICAD = re.compile(r"libki\w*\..+|kicad-cli|_\w+\.kiface|libs3d_plugin_\w+\.so")
BUILT = {re.compile(r"libwx_.+\.dylib"): "wxWidgets", re.compile(r"libngspice\..+"): "ngspice",
         re.compile(r"Python"): "Python"}
NOTICE = """kicad-cli {version} for macOS {arch}, {origin}

{kicad}

Every other library comes unmodified from the component listed below: a Homebrew bottle (the one
holding a file with the library's Mach-O UUID), a Homebrew keg built from source on KiCad's build
machine where no bottle holds the library (its formula and source are those of the same version's
bottle named), or what KiCad's macOS builder, kicad-mac-builder, builds itself. Each component's licence files are in KiCad.app/Contents/Resources/Licenses/<component>/.
The complete corresponding source of each component, with Homebrew's formula and patches, is in
{sources}, attached to the same release.
{credits}
file\tcomponent version\tfrom
{rows}
"""
REPACKAGED_KICAD = ("repackaged from the official KiCad {version} DMG.",
            """KiCad (KiCad.app/Contents/MacOS/kicad-cli, Contents/PlugIns/*.kiface, Contents/PlugIns/3d and
Contents/Frameworks/libki*) is GPL-3.0-or-later. Its source is kicad-{version}-source.tar.gz, attached to the same GitHub release.""")
REBUILT_KICAD = ("the official KiCad {version} DMG with KiCad's own files rebuilt from simee's modified KiCad.",
         """KiCad's own files (KiCad.app/Contents/MacOS/kicad-cli, Contents/PlugIns/*.kiface, Contents/PlugIns/3d and
Contents/Frameworks/libki*) are built from a modified KiCad {version}: simee-kicad commit {sha}
(https://github.com/simee-ai/simee-kicad), which adds changes on top of KiCad's {version} tag (see its
history). KiCad is GPL-3.0-or-later; the modified source is kicad-{version}-source.tar.gz, attached to
the same GitHub release. They are built against exactly the libraries listed below.""")


@dataclass
class ThirdParty:
    components: list[Component] = field(default_factory=list)
    rows: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)  # arch -> (file, component, from)
    licences: dict[str, dict[str, bytes]] = field(default_factory=dict)  # component -> its licence files
    bottles: dict[str, list[homebrew.Bottle]] = field(default_factory=dict)  # arch -> the bottles its files came from
    year: str = ""  # of the KiCad release, for copyright credits


def python_source(version: str, cache: Path, fetch: Fetch) -> Path:
    dest = cache / "python.org" / f"Python-{version}.tar.xz"
    if not dest.exists():
        data = fetch(f"https://www.python.org/ftp/python/{version}/Python-{version}.tar.xz")
        write_atomically(dest, lambda f: f.write(data))
    dest.touch()
    return dest


def _member(archive: Path, rel: str) -> bytes:
    with tarfile.open(archive) as tar:
        return tar.extractfile(next(m for m in tar if m.name.split("/", 1)[-1] == rel)).read()


def _require(needle: bytes, files: list[Path], what: str) -> None:
    if not any(needle in f.read_bytes() for f in files):
        raise RuntimeError(f"{what} doesn't match the bundled {sorted(f.name for f in files)}")


def _built(name: str, files: list[Path], pins, until: str, cache: Path, fetch: Fetch) -> tuple[Component, str]:
    """A component kicad-mac-builder builds, checked against the version string in its binaries."""
    builder = f"kicad-mac-builder {pins.builder[:10]}"
    if name == "Python":
        source = python_source(pins.python, cache, fetch)
        _require(pins.python.encode(), files, f"Python {pins.python}")
        return (Component(name, pins.python, f"Python-{pins.python}", source, ((source.name, source),)),
                f"python.org, made relocatable by {builder}")
    pin = pins.wxwidgets if name == "wxWidgets" else pins.ngspice
    sha = macbuilder.commit(pin, until, fetch)
    source = macbuilder.git_archive(pin, sha, name, cache / "git")
    if name == "wxWidgets":
        h = _member(source, "include/wx/version.h").decode()
        version = ".".join(re.search(rf"#define wx{k}\s+(\d+)", h).group(1)
                           for k in ("MAJOR_VERSION", "MINOR_VERSION", "RELEASE_NUMBER"))
        _require(f"wxWidgets {version}".encode("utf-32-le"), files, f"wxWidgets {version} ({pin.ref} at {sha[:10]})")
    else:
        version = pin.ref.removeprefix("ngspice-")
        _require(b"\0" + version.encode() + b"\0", files, f"ngspice {version} ({pin.ref})")
    return (Component(name, version, f"{name}-{version}-{sha[:12]}", source, ((source.name, source),)),
            f"{pin.url} {pin.ref} at {sha[:10]} ({builder})")


def collect(contents: Path, files: Iterable[Path], version: str, until: str, cache: Path,
            fetch: Fetch = fetch_url, archs: tuple[str, ...] = ("arm64", "x86_64")) -> ThirdParty:
    """The component of every file under a (universal) KiCad.app/Contents, KiCad's own aside, per
    architecture, with their sources fetched into cache. until: when KiCad published the release."""
    brewed: dict[str, list[Path]] = defaultdict(list)
    built: dict[str, list[Path]] = defaultdict(list)
    unknown = []
    for f in files:
        if KICAD.fullmatch(f.name):
            continue
        if name := homebrew.formula(f.name):
            brewed[name].append(f)
        elif name := next((n for p, n in BUILT.items() if p.fullmatch(f.name)), None):
            built[name].append(f)
        else:
            unknown.append(str(f.relative_to(contents)))
    if unknown:
        raise RuntimeError(f"libraries of unknown provenance, so their licence and source are unknown "
                           f"(add them to homebrew.FORMULAE if Homebrew ships them): {unknown}")

    third, rows = ThirdParty(), defaultdict(list)
    components: dict[tuple[str, str], Component] = {}
    for name, libs in sorted(brewed.items()):
        slices = {f: macho.slices(f.read_bytes()) for f in libs}
        for arch in archs:
            if missing := [f.name for f in libs if arch not in slices[f]]:
                raise RuntimeError(f"no {arch} code in {missing}")
            bottle = homebrew.match(name, arch, {f.name: slices[f][arch] for f in libs}, until,
                                    cache / HOMEBREW_BOTTLES, fetch)
            third.bottles.setdefault(arch, []).append(bottle)
            key = (name, bottle.version)
            if key not in components:
                srcs = tuple(homebrew.sources(bottle, cache / MACOS_SOURCES, fetch))
                components[key] = Component(name, bottle.version, f"{name}-{bottle.version}", srcs[0][1], srcs)
            ref = f"{bottle.tag} bottle, homebrew-core {bottle.commit[:10]}"
            origin = (f"Homebrew {bottle.built_for} keg built from source, formula and source as in its {ref}"
                      if bottle.built_for else f"Homebrew {ref}")
            rows[arch] += [(str(f.relative_to(contents)), f"{name} {bottle.version}", origin) for f in libs]
    if built:
        pins = macbuilder.pins(version, until, fetch)
        for name, libs in sorted(built.items()):
            component, origin = _built(name, libs, pins, until, cache / MACOS_SOURCES, fetch)
            components[(name, component.version)] = component
            for arch in archs:
                rows[arch] += [(str(f.relative_to(contents)), f"{name} {component.version}", origin) for f in libs]
    third.components = sorted(components.values(), key=lambda c: (c.name, c.version))
    third.rows = {arch: sorted(r) for arch, r in rows.items()}
    third.licences = {c.name: licences(c.licence_source) for c in third.components}
    third.year = until[:4]
    return third


def write_notices(third: ThirdParty, root: Path, arch: str, version: str, sources: str,
                  simee_sha: str | None = None) -> None:
    """root/THIRD-PARTY.txt, and each component's licence files under KiCad.app/Contents/Resources/Licenses/."""
    third_party.write_licences(third.licences, root / "KiCad.app/Contents/Resources/Licenses")
    credits = third_party.credits((c.name for c in third.components), third.year)
    rows = "\n".join(f"KiCad.app/Contents/{f}\t{c}\t{o}" for f, c, o in third.rows[arch])
    origin, kicad = REBUILT_KICAD if simee_sha else REPACKAGED_KICAD
    fill = {"version": version, "sha": simee_sha}
    (root / "THIRD-PARTY.txt").write_text(NOTICE.format(version=version, arch=arch, sources=sources, credits=credits,
                                                        rows=rows, origin=origin.format(**fill),
                                                        kicad=kicad.format(**fill)))
