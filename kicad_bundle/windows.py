"""Windows: official NSIS installer -> trimmed kicad-cli folder (zip), with the licences of its
third-party DLLs, and one archive of their sources (see windows_third_party). With a simee-kicad
branch, KiCad's own files are built from it (windows_build)."""

import os
import shutil
import subprocess
from pathlib import Path

from kicad_bundle import imports, pe, release, simee_source, smoke, third_party, windows_build, windows_third_party
from kicad_bundle.bundle import KIFACES, archive
from kicad_bundle.closure import closure

# Data kicad-cli reads at startup (it logs an error without the API schema).
DATA = ("share/kicad/schemas",)


def _seven_zip() -> str:
    for name in ("7z", "7zz", "7za"):
        if found := shutil.which(name):
            return found
    raise RuntimeError("7-Zip (7z) is needed to unpack the KiCad installer")


def package(version: str, out_dir: Path, cache: Path, work: Path, run_smoke: bool = True,
            arch: str = "x86_64", simee_ref: str | None = None) -> list[Path]:
    """The bundle and its third-party sources. With simee_ref (a simee-kicad branch such as
    simee/10.0.6), KiCad's own files are built from it, and its source tarball is one of the results."""
    sha = simee_source.resolve_ref(simee_ref) if simee_ref else None
    name = f"kicad-{version}-{arch}.exe"
    found = release.installer(version, name, f"windows/stable/{name}")
    installer = release.cached(found, version, cache)
    extracted = work / f"windows-{arch}-installer"
    # Everything kicad-cli needs is in the installer's bin/ plus DATA (0.5 GB vs 4.5 GB for all of
    # it); fall back to a full extraction if a future installer moves it.
    for only in (["bin", *DATA], []):
        if extracted.exists():
            shutil.rmtree(extracted)
        subprocess.run([_seven_zip(), "x", "-y", f"-o{extracted}", str(installer), *only], check=True,
                       stdout=subprocess.DEVNULL)
        cli = next(extracted.rglob("kicad-cli.exe"), None)
        if cli:
            break
    else:
        raise RuntimeError("kicad-cli.exe not found in the installer")
    kifaces = [next(cli.parent.glob(f"_{k}.dll"), None) or next(extracted.rglob(f"_{k}.dll")) for k in KIFACES]
    plugins = [cli.parent / p for p in windows_build.PLUGINS]  # dlopened: no import reaches them
    if missing := [p.name for p in plugins if not p.is_file()]:
        raise RuntimeError(f"the installer has no {', '.join(missing)} next to kicad-cli.exe")
    keep = closure([cli, *kifaces, *plugins], deps=pe.deps,
                   resolve=pe.make_resolver(sorted({cli.parent, *(k.parent for k in kifaces)})))

    root = work / f"kicad-cli-{version}-windows-{arch}"
    if root.exists():
        shutil.rmtree(root)
    for f in keep:
        dest = root / f.relative_to(extracted)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    for data in DATA:
        if (extracted / data).is_dir():
            shutil.copytree(extracted / data, root / data)
    print(f"  {len(keep)} files from the installer")
    sources = f"{root.name}-sources.tar"
    third = windows_third_party.collect(root, [root / f.relative_to(extracted) for f in keep], version,
                                        found.published_at[:4], cache)
    windows_third_party.write_notices(third, root, version, sources, sha)
    bundled_cli = root / cli.relative_to(extracted)
    extra = []
    if sha:
        print(f"  building KiCad's own files from simee-kicad {sha} ({simee_ref})")
        src = simee_source.source_archive(sha, out_dir / f"kicad-{version}-source.tar.gz")
        built = windows_build.build(src, work, cache, windows_build.toolset(bundled_cli),
                                    windows_build.vcpkg_commit(found.published_at))
        print("  replaced " + ", ".join(windows_build.overlay(bundled_cli.parent, built)))
        extra.append(src)

    if run_smoke:
        if os.name == "nt":
            smoke.check([str(bundled_cli)])
            if sha:
                imports.check([str(bundled_cli)])
            print(f"  smoke test passed{' (sch and pcb import too)' if sha else ''}")
        else:
            print("  smoke test skipped: needs Windows")
    return [archive(root, out_dir, "zip"), third_party.sources_archive(third.components, out_dir / sources), *extra]
