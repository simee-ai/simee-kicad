"""Windows: build KiCad's own binaries from a simee/<version> branch and lay them over the official bundle.

KiCad's Windows release is built by kicad-win-builder (build.ps1): MSVC, and vcpkg in manifest mode
from the source tree's vcpkg.json and vcpkg-configuration.json, which pin every port's version. The
branch keeps those files, so the same vcpkg tool commit builds the same ports, and KiCad is configured
with build.ps1's options. Only KiCad's own files (kicad-cli.exe, the kifaces _*.dll, the 3D plugins, ki*.dll) are replaced;
the third-party DLLs, their notices and their sources stay as windows.package made them. So our files
must be linked by the MSVC version that linked the official ones (the bundle keeps KiCad's C++ runtime,
which must be at least as new) and import nothing the official ones didn't.
"""

import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pefile

from kicad_bundle import bundle, gitlab, pe
from kicad_bundle.bundle import KIFACES, PLUGINS_3D
from kicad_bundle.cache import VCPKG_BINARIES
from kicad_bundle.fetch import Fetch, cached_file, fetch_url
from kicad_bundle.windows_third_party import KICAD

VCPKG = "https://github.com/microsoft/vcpkg"
BUILDER = "kicad/packaging/kicad-win-builder"
TRIPLET = "x64-windows"
# build.ps1's KiCad options, less translations (the bundle has none) and Sentry (KiCad's crash reports).
CMAKE_FLAGS = ("-Wno-dev", "-DCMAKE_BUILD_TYPE=Release", "-DKICAD_BUILD_QA_TESTS=OFF", "-DKICAD_BUILD_I18N=OFF",
               "-DKICAD_WIN32_DPI_AWARE=ON", "-DKICAD_SCRIPTING_WXPYTHON=ON")
# kicad-cli, the kifaces and the 3D plugins; they pull in kicommon, kigal, kiapi and kicad_3dsg.
TARGETS = ("kicad-cli", *(f"{k}_kiface" for k in KIFACES), *(f"s3d_plugin_{p}" for p in PLUGINS_3D))
# The 3D plugins, relative to bin\ (KiCad looks for them next to its executable, in plugins\3d).
PLUGINS = tuple(f"plugins/3d/s3d_plugin_{p}.dll" for p in PLUGINS_3D)
# KiCad's configure needs SWIG (pcbnew's Python bindings); build.ps1 puts this one on PATH.
SWIG_URL = ("https://sourceforge.net/projects/swig/files/swigwin/swigwin-4.3.1/swigwin-4.3.1.zip/download"
            "?use_mirror=pilotfiber")
SWIG_SHA256 = "7ea5197c557af20b2f7780ffcfe803bbe0e2009f5846874112aea37e5f693417"
VSWHERE = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"


def cmake_args(src: Path, build_dir: Path, vcpkg: Path) -> list[str]:
    return ["-G", "Ninja", "-S", str(src), "-B", str(build_dir), *CMAKE_FLAGS,
            f"-DCMAKE_TOOLCHAIN_FILE={vcpkg / 'scripts/buildsystems/vcpkg.cmake'}", f"-DVCPKG_TARGET_TRIPLET={TRIPLET}",
            "-DVCPKG_INSTALL_OPTIONS=--clean-after-build"]


def vcpkg_commit(until: str, fetch: Fetch = fetch_url) -> str:
    """The vcpkg tool and cmake scripts (the ports themselves come from KiCad's manifest) as build.ps1
    pinned them by until, when KiCad published the installer: KiCad builds releases and RCs from master."""
    commit = gitlab.head_at(BUILDER, "master", until, fetch)
    found = re.search(r'^\$vcpkgCommit\s*=\s*"([0-9a-f]{40})"', gitlab.file_at(BUILDER, "build.ps1", commit, fetch), re.M)
    if not found:
        raise RuntimeError(f"{BUILDER} {commit}: build.ps1 sets no $vcpkgCommit")
    return found.group(1)


def toolset(binary: Path) -> str:
    """The MSVC version (major.minor, e.g. 14.44) whose linker made binary."""
    image = pefile.PE(str(binary), fast_load=True)
    try:
        return f"{image.OPTIONAL_HEADER.MajorLinkerVersion}.{image.OPTIONAL_HEADER.MinorLinkerVersion}"
    finally:
        image.close()


def parse_env(text: str) -> dict[str, str]:
    """The environment `set` prints."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("="))


def overlay(bin_dir: Path, built: Path) -> list[str]:
    """Replace every KiCad file in bin_dir and its plugins/3d with built's; returns their paths relative to
    bin_dir. Refuses to leave any
    official, or to ship one linked by another MSVC or importing a DLL no official file in bin_dir
imports (one the bundle lacks, or a Windows DLL it never relied on). Windows API sets, api-ms-win-*,
aside: Windows resolves them itself, which ones a file names depends on the Windows SDK (the official
_cvpcb.dll reaches the kernel only through them, ours through KERNEL32.dll), and the smoke test
proves they load."""
    plugins = bin_dir / Path(PLUGINS[0]).parent
    files = sorted(p for d in (bin_dir, plugins) if d.is_dir() for p in d.iterdir() if p.is_file())
    names = [p.relative_to(bin_dir).as_posix() for p in files if KICAD.fullmatch(p.name)]
    official = {d.lower() for p in files if p.suffix.lower() in (".exe", ".dll") for d in pe.deps(p)}
    problems = []
    for name in (n for n in names if (built / Path(n).name).is_file()):
        ours, theirs = toolset(built / Path(name).name), toolset(bin_dir / name)
        if ours != theirs:
            problems.append(f"{name} was linked by MSVC {ours}, the official one by {theirs}")
        if added := [d for d in pe.deps(built / Path(name).name)
                     if d.lower() not in official and not pe.UCRT.fullmatch(d)]:
            problems.append(f"{name} imports {', '.join(added)}, which no official file does")
    if problems:
        raise RuntimeError("the simee build doesn't match the official one:\n  " + "\n  ".join(problems))
    return bundle.overlay(bin_dir, names, built)


def collect_built(build_dir: Path, out: Path) -> Path:
    """Copy KiCad's binaries out of the build tree (not vcpkg's) into out; each must be built once."""
    found: dict[str, list[Path]] = {}
    for p in build_dir.rglob("*"):
        if p.suffix.lower() in (".exe", ".dll") and KICAD.fullmatch(p.name) and p.is_file() \
                and "vcpkg_installed" not in p.relative_to(build_dir).parts:
            found.setdefault(p.name, []).append(p)
    if twice := {n: ps for n, ps in found.items() if len(ps) > 1}:
        raise RuntimeError(f"built more than once: {twice}")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, (path,) in found.items():
        shutil.copy2(path, out / name)
    return out


def _in_tree(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """tar's data filter, but a link out of the tree (qa/tests/resources -> an absolute path in KiCad's
    source) is skipped: the build never follows one."""
    try:
        return tarfile.data_filter(member, path)
    except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
        return None


def _unpack(src: Path, dest: Path) -> None:
    """GitHub's source tarball into dest, without its top-level folder."""
    if dest.exists():
        shutil.rmtree(dest)
    with tarfile.open(src) as tar:
        members = []
        for m in tar.getmembers():
            m.name = m.name.partition("/")[2]
            if m.name:
                members.append(m)
        tar.extractall(dest, members=members, filter=_in_tree)


def msvc_env(version: str) -> dict[str, str]:
    """The environment of Visual Studio 2022's x64 prompt with MSVC version (e.g. 14.44)."""
    vs = subprocess.run([str(VSWHERE), "-latest", "-products", "*", "-version", "[17.0,18.0)", "-requires",
                         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                        capture_output=True, text=True, check=True).stdout.strip()
    if not vs:
        raise RuntimeError("Visual Studio 2022 with the C++ x64 tools is needed")
    vcvars = Path(vs) / "VC/Auxiliary/Build/vcvars64.bat"
    out = subprocess.run(f'"{vcvars}" -vcvars_ver={version} >nul && set', shell=True, capture_output=True, text=True)
    env = parse_env(out.stdout)
    if not env.get("VCToolsVersion", "").startswith(f"{version}."):
        raise RuntimeError(f"MSVC {version} isn't installed in {vs}: {(out.stdout + out.stderr).strip()[-1000:]}")
    return env


def swig(work: Path, fetch: Fetch = fetch_url, digest: str = SWIG_SHA256) -> Path:
    """build.ps1's swigwin, unpacked under work; returns the folder holding swig.exe."""
    archive = cached_file("swigwin.zip", SWIG_URL, digest, work / "downloads", fetch)
    dest = work / "swigwin"
    if dest.exists():
        shutil.rmtree(dest)
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)
    return next(dest.rglob("swig.exe")).parent


def _vcpkg(root: Path, commit: str) -> Path:
    """vcpkg at commit, bootstrapped."""
    if not (root / ".git").is_dir():
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != commit:
        subprocess.run(["git", "-C", str(root), "fetch", "-q", "--depth", "1", VCPKG, commit], check=True)
        subprocess.run(["git", "-C", str(root), "checkout", "-q", "-f", commit], check=True)
        (root / "vcpkg.exe").unlink(missing_ok=True)  # the tool version belongs to the scripts
    if not (root / "vcpkg.exe").exists():
        subprocess.run([str(root / "bootstrap-vcpkg.bat"), "-disableMetrics"], check=True)
    return root


def build(src: Path, work: Path, cache: Path, version: str, vcpkg_at: str) -> Path:
    """Build kicad-cli, the kifaces and the 3D plugins from src (a source tarball) with MSVC version (the official
    build's) and vcpkg at commit vcpkg_at (vcpkg_commit); returns the folder holding KiCad's binaries.
    vcpkg's builds of the ports are kept in <cache>/vcpkg-binaries (several GB), so a rebuild only compiles KiCad (about an hour)."""
    if os.name != "nt":
        raise RuntimeError("building KiCad for Windows needs Windows with Visual Studio 2022 "
                           f"(MSVC {version}); the package workflow's Windows job does it")
    tree, build_dir = work / "kicad-src", work / "kb"  # short: vcpkg and KiCad nest deep paths
    _unpack(src, tree)
    if build_dir.exists():
        shutil.rmtree(build_dir)
    binaries = cache / VCPKG_BINARIES
    binaries.mkdir(parents=True, exist_ok=True)
    vcpkg = _vcpkg(work / "vcpkg", vcpkg_at)
    env = {**msvc_env(version), "VCPKG_ROOT": str(vcpkg), "VCPKG_DISABLE_METRICS": "1",
           "VCPKG_BINARY_SOURCES": f"clear;files,{binaries},readwrite"}
    path = env.pop("Path", None) or env.pop("PATH", "")
    env["PATH"] = f"{swig(work)};{path}"
    cmake = shutil.which("cmake", path=env["PATH"])
    if not cmake:
        raise RuntimeError("cmake isn't on Visual Studio's PATH (install its C++ CMake tools)")
    subprocess.run([cmake, *cmake_args(tree, build_dir, vcpkg)], env=env, check=True)
    subprocess.run([cmake, "--build", str(build_dir), "--target", *TARGETS], env=env, check=True)
    return collect_built(build_dir, work / "windows-built")
