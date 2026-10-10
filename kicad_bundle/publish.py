"""Release bookkeeping for the package workflow: tag numbering, checksums and notes.

    python -m kicad_bundle.publish --kicad-version 10.0.6 --dist dist --tags tags.txt --run-url URL

writes dist/SHA256SUMS and notes.md and prints the release tag.
"""

import argparse
import hashlib
import re
from pathlib import Path
from typing import Callable


def next_tag(version: str, existing: list[str], prefix: str = "cli") -> str:
    """<prefix>-<version>-<n>: n counts our builds of that version (cli: kicad-cli of a KiCad release)."""
    pattern = rf"{re.escape(prefix)}-{re.escape(version)}-(\d+)"
    builds = [int(m.group(1)) for t in existing if (m := re.fullmatch(pattern, t))]
    return f"{prefix}-{version}-{max(builds, default=0) + 1}"


def sha256sums(dist: Path) -> str:
    lines = []
    for path in sorted(p for p in dist.iterdir() if p.is_file() and p.name != "SHA256SUMS"):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    return "\n".join(lines) + "\n"



def toolchain_main(argv, version: str, prefix: str, notes: Callable[[str, str], str]) -> int:
    """The avr-gcc and arm-gcc workflows' release step: writes <dist>/SHA256SUMS and the notes
    (notes(run_url, sums)) and prints the release tag, <prefix>-<version>-<n>."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--tags", type=Path, required=True, help="file with existing release tags, one per line")
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--notes", type=Path, default=Path("notes.md"))
    args = parser.parse_args(argv)

    sums = sha256sums(args.dist)
    (args.dist / "SHA256SUMS").write_text(sums)
    args.notes.write_text(notes(args.run_url, sums))
    print(next_tag(version, args.tags.read_text().split(), prefix))
    return 0

def release_notes(version: str, run_url: str, assets: list[str], simee_sha: str | None = None) -> str:
    """simee_sha: the simee-kicad commit KiCad's own files were built from (none: official binaries)."""
    listed = "\n".join(f"- `{a}`" for a in sorted(assets))
    if simee_sha:
        commit = f"https://github.com/simee-ai/simee-kicad/commit/{simee_sha}"
        origin = (f"KiCad {version} with simee's changes (`kicad-cli sch import` among them), its own files built\n"
                  f"from simee-kicad {commit}, on the official release's libraries")
        source = f"`kicad-{version}-source.tar.gz` is attached: the source of {commit}"
    else:
        origin = f"the official, unmodified KiCad {version} release"
        source = (f"`kicad-{version}-source.tar.gz` is attached, and the same tag is upstream at\n"
                  f"https://gitlab.com/kicad/code/kicad/-/tags/{version}")
    return f"""Trimmed `kicad-cli` from {origin}: just what
`kicad-cli sch ...`, `fp ...` and `pcb ...` need (the schematic, footprint-assignment and PCB modules, so
ERC, footprint upgrades, gerbers, drill, STEP, VRML and SVG exports, headless 3D renders and board imports
work, and their shared libraries; and the plugins that load the 3D models a board brings, STEP, VRML and IDF),
re-signed ad hoc on macOS.
Unpack and run `kicad-cli` (macOS: `KiCad.app/Contents/MacOS/kicad-cli`; Windows 10 or newer: `bin\\kicad-cli.exe`;
Linux x86_64 or arm64: `bin/kicad-cli`, which needs glibc 2.39 or newer, e.g. Ubuntu 24.04 or Debian 13).
Set `KICAD_CONFIG_HOME`, `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` to keep it out of the user's home.

The Linux binaries come from the official `kicad/kicad:{version}` Docker image, with every library but
glibc in `lib/`. Its `THIRD-PARTY.txt` names the Debian package each library comes from, with that
package's licence in `share/doc/<package>/copyright`; `kicad-cli-{version}-linux-x86_64-sources.tar`
holds the exact Debian source of every one of them. The image is amd64 only, so the arm64 bundle is built
on the Debian image it is built from, for arm64, with its Debian packages at the same versions; its sources are
`kicad-cli-{version}-linux-arm64-sources.tar`.

The macOS libraries come from Homebrew bottles (each matched to its bottle by Mach-O UUID) and from
what KiCad's macOS builder builds itself (its wxWidgets fork, ngspice, Python). Each macOS bundle's
`THIRD-PARTY.txt` names the component of every library, with its licence files in
`KiCad.app/Contents/Resources/Licenses/<component>/`; `kicad-cli-{version}-macos-sources.tar` (both
architectures) holds the source of every one of them, with Homebrew's formula and patches.

The Windows DLLs are built by KiCad with vcpkg, from the ports and versions KiCad's source pins. The Windows
bundle's `THIRD-PARTY.txt` names the vcpkg port of every DLL, with its licence files in `share\\doc\\<port>\\`;
`kicad-cli-{version}-windows-x86_64-sources.tar` holds the upstream source archives each port downloads, with
the port itself (portfile and patches). The Microsoft Visual C++ runtime DLLs are Microsoft's redistributable
files, shipped as KiCad ships them.

{listed}

Source: {source}. KiCad is GPL-3.0-or-later; the bundled
third-party libraries keep their own licenses.

Built by {run_url}
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kicad-version", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--tags", type=Path, required=True, help="file with existing release tags, one per line")
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--notes", type=Path, default=Path("notes.md"))
    parser.add_argument("--simee-sha", help="simee-kicad commit KiCad's own files were built from")
    args = parser.parse_args(argv)

    (args.dist / "SHA256SUMS").write_text(sha256sums(args.dist))
    assets = [p.name for p in args.dist.iterdir() if p.is_file()]
    args.notes.write_text(release_notes(args.kicad_version, args.run_url, assets, args.simee_sha))
    print(next_tag(args.kicad_version, args.tags.read_text().split()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
