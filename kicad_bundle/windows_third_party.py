"""Licences and sources of the third-party DLLs in the Windows bundle. KiCad builds them with vcpkg
(see vcpkg.py): each DLL names the port it was built in, the port's portfile names the upstream
sources (see portfile.py), and the port folder holds the patches. The Microsoft C++ runtime comes
from Visual Studio. A DLL that is none of these, nor KiCad's own, fails the build."""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from kicad_bundle import pe, portfile, third_party, vcpkg
from kicad_bundle.cache import VCPKG_REGISTRIES, WINDOWS_SOURCES
from kicad_bundle.fetch import Fetch, cached_file, fetch_url, write_atomically
from kicad_bundle.third_party import Component
from kicad_bundle.vcpkg import GitRepo, Port, Registry

# KiCad's own files: its source (a separate release asset) covers them.
KICAD = re.compile(r"kicad-cli\.exe|_\w+\.dll|ki\w*\.dll|s3d_plugin_\w+\.dll", re.I)
# The Visual C++ runtime, Distributable Code of Visual Studio 2022.
MSVC = re.compile(r"(vcruntime140(_\d)?|msvcp140(_\w+)?|concrt140|vccorlib140)\.dll", re.I)
MSVC_TERMS = "https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution#visual-c-runtime-files"

NOTICE = """kicad-cli {version} for Windows x86_64, repackaged from the official KiCad {version} installer.{simee}

KiCad (bin/kicad-cli.exe, bin/_*.dll and bin/ki*.dll) is GPL-3.0-or-later. Its source is
kicad-{version}-source.tar.gz, attached to the same GitHub release.

KiCad builds every other library with vcpkg, from the ports and versions its source pins
(vcpkg.json and vcpkg-configuration.json at the release tag). Each comes unmodified from the vcpkg
port listed below, and each port's licence files are in share/doc/<port>/. The complete
corresponding source of each port, that is the upstream archives its portfile downloads and the port
itself (portfile and patches, in port/), is in
{sources}, attached to the same release.
{microsoft}{credits}
file\tport version\tfrom
{rows}
"""

MICROSOFT = """
The Microsoft Visual C++ runtime {version} ({files}) is redistributed unmodified, as KiCad
ships it, under the Distributable Code terms of Visual Studio 2022 ("Visual C++ Runtime Files"):
{terms}
It is Microsoft's and not open source. The Universal CRT isn't shipped: Windows 10 and later, which
this kicad-cli needs, always use their own.
"""

SIMEE = """
KiCad's own files (bin/kicad-cli.exe, bin/_*.dll and bin/ki*.dll) are KiCad {version}
with simee's changes, built from simee-kicad commit {sha}
with the MSVC version and vcpkg ports of the official build."""


@dataclass
class ThirdParty:
    components: list[Component] = field(default_factory=list)
    rows: list[tuple[str, str, str]] = field(default_factory=list)  # (file, port version, from)
    licences: dict[str, dict[str, bytes]] = field(default_factory=dict)  # port -> its licence files
    microsoft: list[tuple[str, str]] = field(default_factory=list)  # (file, version) of the C++ runtime
    year: str = ""  # of the KiCad release, for copyright credits


def _numbers(version: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", version)]


def _matches(port_version: str, info: dict[str, str]) -> bool | None:
    """Whether a DLL's FileVersion or ProductVersion starts with the port's version (None: it has neither)."""
    want = _numbers(port_version)
    found = [_numbers(info[k]) for k in ("FileVersion", "ProductVersion") if k in info]
    return any(f[:len(want)] == want for f in found) if found else None


def _cached(port: Port, rel: str, data: bytes, cache: Path) -> Path:
    """A file of a port folder, stored by the port's tree id (touched on reuse, for cache.prune)."""
    path = cache / "ports" / port.tree / rel
    if path.exists():
        path.touch()
    else:
        write_atomically(path, lambda f: f.write(data))
    return path


def _download(d: portfile.Download, cache: Path, fetch: Fetch) -> Path:
    for i, url in enumerate(d.urls):
        try:
            return cached_file(d.filename, url, d.sha512, cache, fetch, "sha512")
        except Exception:
            if i == len(d.urls) - 1:
                raise
    raise AssertionError("unreachable")


def _origin(port: Port) -> str:
    return (f"vcpkg port, {port.registry.url.split('://', 1)[-1].removesuffix('.git')} "
            f"{port.registry.baseline[:10]} (tree {port.tree[:10]})")


def collect(root: Path, files: Iterable[Path], version: str, year: str, cache: Path, fetch: Fetch = fetch_url,
            repo: Callable[[Registry], GitRepo] | None = None) -> ThirdParty:
    """The vcpkg port of every DLL under root (KiCad's own and the C++ runtime aside), with each port's
    sources fetched into cache. year: of the KiCad release."""
    repos: dict[Registry, GitRepo] = {}
    repo = repo or (lambda r: repos.setdefault(r, GitRepo(r.url, cache / VCPKG_REGISTRIES)))
    third = ThirdParty(year=year)
    built: dict[str, list[Path]] = defaultdict(list)
    unknown, not_microsoft = [], []
    for f in sorted(files):
        if MSVC.fullmatch(f.name):
            info = pe.version_info(f)
            if info.get("CompanyName") != "Microsoft Corporation":
                not_microsoft.append(f.name)
            third.microsoft.append((f.relative_to(root).as_posix(), info.get("FileVersion", "?")))
            continue
        ports = vcpkg.built_ports(f.read_bytes())
        if not ports and not KICAD.fullmatch(f.name):
            unknown.append(f.relative_to(root).as_posix())
        for name in ports:
            built[name].append(f)
    if unknown or not_microsoft:
        raise RuntimeError(f"DLLs of unknown provenance, so their licence and source are unknown (no vcpkg build "
                           f"path in them): {unknown}; C++ runtime DLLs not from Microsoft: {not_microsoft}")

    config = vcpkg.kicad_config(version, fetch)
    problems = []
    for name, dlls in sorted(built.items()):
        port = vcpkg.port(config, name, repo)
        downloads = portfile.downloads(port.files["portfile.cmake"].decode(), port.version)
        fingerprints = {vcpkg.fingerprint(d, port.files) for d in downloads}
        archives = [(d.filename, _download(d, cache / WINDOWS_SOURCES, fetch)) for d in downloads]
        for dll in dlls:
            dirs = {h for p, h in vcpkg.source_dirs(dll.read_bytes()) if p == name}
            if dirs - fingerprints:
                problems.append(f"{dll.name} was built from {name} sources {sorted(dirs)}, but port {name} "
                                f"{port.version}#{port.port_version} gives {sorted(fingerprints - {None})}")
            elif not dirs and _matches(port.version, pe.version_info(dll)) is False:
                problems.append(f"{dll.name} is version {pe.version_info(dll)}, but port {name} is {port.version}")
        port_files = [(f"port/{rel}", _cached(port, rel, data, cache / WINDOWS_SOURCES))
                      for rel, data in sorted(port.files.items())]
        main = next((a for d, a in zip(downloads, archives) if d.main), archives[0] if archives else None)
        if main is None:
            problems.append(f"port {name} downloads no source")
            continue
        component = Component(name, f"{port.version}#{port.port_version}", f"{name}-{port.version}_{port.port_version}",
                              main[1], (*archives, *port_files))
        third.components.append(component)
        third.licences[name] = third_party.licences(component.licence_source)
        third.rows += [(dll.relative_to(root).as_posix(), f"{name} {component.version}", _origin(port)) for dll in dlls]
    if problems:
        raise RuntimeError("KiCad's DLLs don't match the vcpkg ports its source pins:\n  " + "\n  ".join(problems))
    third.rows.sort()
    return third


def write_notices(third: ThirdParty, root: Path, version: str, sources: str, simee_sha: str | None = None) -> None:
    """root/THIRD-PARTY.txt, and each port's licence files under root/share/doc/<port>/. simee_sha: the
    simee-kicad commit KiCad's own files were built from, if they were."""
    third_party.write_licences(third.licences, root / "share/doc")
    microsoft = ""
    if third.microsoft:
        files = ", ".join(Path(f).name for f, _ in third.microsoft)
        microsoft = MICROSOFT.format(files=files, version=third.microsoft[0][1], terms=MSVC_TERMS)
    rows = "\n".join("\t".join(r) for r in third.rows)
    (root / "THIRD-PARTY.txt").write_text(NOTICE.format(
        version=version, sources=sources, microsoft=microsoft,
        simee=SIMEE.format(version=version, sha=simee_sha) if simee_sha else "",
        credits=third_party.credits((c.name for c in third.components), third.year), rows=rows))
