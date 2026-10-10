"""KiCad's own macOS binaries (kicad-cli, the kifaces, the 3D plugins, libki*) built from a simee/<version> source tree
for one architecture, to replace the official ones in a repackaged DMG. Everything else in the bundle
stays the official build, so they are built against exactly what it ships: the Homebrew bottles its
libraries came from (poured into a private prefix, see brew_prefix.py), kicad-mac-builder's wxWidgets
built the way it builds it, and the DMG's own Python and ngspice. Then each built file gets the install
names and rpaths of the official file it replaces, and every symbol it imports from a bundled library
must be exported by one."""

import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path

from kicad_bundle import brew_prefix, homebrew, macbuilder, macho
from kicad_bundle.bundle import remove
from kicad_bundle.cache import HOMEBREW_BOTTLES, MACOS_SOURCES
from kicad_bundle.fetch import Fetch
from kicad_bundle.homebrew import Bottle

# Not in the bundle, but KiCad's CMake requires them: formula -> the official app's files from its bottle
# (matched by UUID like the bundled libraries), or None for a header-only formula (its bottle at the release).
# (opencascade, which it needs too, is in the bundle: pcbnew links it.)
BUILD_ONLY = {"glm": None}
JOBS = os.cpu_count() or 4
HOST_PREFIXES = ("/opt/homebrew/", "/usr/local/")  # Homebrew's: a build must use none of it


def top_dir(folder: Path) -> Path:
    """The one folder an archive unpacked into folder (ignoring Finder's .DS_Store)."""
    dirs = [d for d in folder.iterdir() if d.is_dir()]
    if len(dirs) != 1:
        raise RuntimeError(f"expected one folder in {folder}, found {dirs}")
    return dirs[0]


def unpack(tarball: Path, dest: Path) -> Path:
    """The source tree of a source tarball, unpacked afresh into dest."""
    if dest.exists():
        remove(dest)
    dest.mkdir(parents=True)
    with tarfile.open(tarball) as tar:
        tar.extractall(dest, filter="tar")
    return top_dir(dest)


def dyld_name(install_name: str) -> str:
    """How `dyld_info` names a library: the leaf without .dylib and its last version component."""
    leaf = install_name.rsplit("/", 1)[-1]
    if leaf.endswith(".dylib"):
        leaf = leaf.removesuffix(".dylib").rsplit(".", 1)[0]
    return leaf


def parse_dyld_info(output: str, section: str) -> list[tuple[str, str | None]]:
    """(symbol, library it comes from) of each import (`-imports`), or (symbol, None) of each export."""
    found, inside = [], False
    for line in output.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].startswith("-"):
            inside = words[0] == f"{section}:"
        elif inside and section == "-imports" and (m := re.fullmatch(r"\s*(\S+)\s+\(from (.+)\)", line)):
            found.append((m.group(1), m.group(2)))
        elif inside and section == "-exports" and len(words) >= 2 and words[0].startswith("0x"):
            found.append((words[1], None))
    return found


def missing_imports(imports: list[tuple[str, str]], system: set[str], exported: set[str]) -> list[tuple[str, str]]:
    """Imports from a bundled library (not a system one, nor dyld's flat namespace) that none exports."""
    return [(sym, lib) for sym, lib in imports
            if lib not in system and not lib.startswith("<") and sym not in exported]


def relink_plan(ours: list[str], official: list[str]) -> dict[str, str]:
    """Our binary's dependency -> the install name the official binary uses for the same file."""
    by_leaf = {name.rsplit("/", 1)[-1]: name for name in official}
    plan = {}
    for name in ours:
        if name.startswith(macho.SYSTEM_PREFIXES):
            continue
        if name.startswith(HOST_PREFIXES):
            raise RuntimeError(f"built binary links {name} from the host's Homebrew, not the poured prefix")
        leaf = name.rsplit("/", 1)[-1]
        if leaf not in by_leaf:
            raise RuntimeError(f"built binary links {leaf}, which the official one doesn't: the bundle lacks it")
        if by_leaf[leaf] != name:
            plan[name] = by_leaf[leaf]
    return plan


def targets(files: list[str]) -> list[str]:
    """The ninja targets that build these bundle files (KiCad's libraries are built as their dependencies)."""
    found = set()
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        if name.endswith(".kiface"):
            found.add(name.removeprefix("_").removesuffix(".kiface") + "_kiface")
        elif rel.startswith("PlugIns/3d/"):
            found.add(name.removeprefix("lib").removesuffix(".so"))
        elif rel.startswith("MacOS/"):
            found.add(name)
    return sorted(found)


# ---- building ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Official:
    """The official app the build is overlaid on (one architecture of it is built)."""
    contents: Path  # its KiCad.app/Contents
    minos: str  # the macOS its binaries target, e.g. 11.6
    pins: macbuilder.Pins
    until: str  # when KiCad published the release


def _sdk() -> str:
    return subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True, check=True).stdout.strip()


def _run(args: list[str], cwd: Path | None = None, env: dict | None = None, log: Path | None = None) -> None:
    """Run a build step; its output goes to log (the tail is shown when it fails)."""
    print(f"  $ {' '.join(args)[:200]}", flush=True)
    with open(log or os.devnull, "a") as out:
        result = subprocess.run(args, cwd=cwd, env={**os.environ, "SDKROOT": _sdk(), **(env or {})},
                                stdout=out, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        tail = log.read_text(errors="replace").splitlines()[-40:] if log else []
        raise RuntimeError(f"{args[0]} failed ({result.returncode}); last lines of {log}:\n" + "\n".join(tail))


def _toolchain() -> Path:
    """Xcode's clang, called directly: the /usr/bin shims put /usr/local/include ahead of every -isystem
    dir, where an Intel Homebrew's headers would win over the prefix's."""
    developer = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, check=True).stdout.strip()
    return Path(developer) / "Toolchains/XcodeDefault.xctoolchain/usr/bin"


def _build_only(official: Official, arch: str, tag: str, cache: Path, fetch: Fetch) -> list[Bottle]:
    found = []
    for name, pattern in BUILD_ONLY.items():
        if pattern is None:
            found.append(homebrew.bottle_at(name, tag, official.until, cache / HOMEBREW_BOTTLES, fetch))
        else:
            libs = {f.name: macho.slices(f.read_bytes())[arch] for f in official.contents.glob(pattern)}
            found.append(homebrew.match(name, arch, libs, official.until, cache / HOMEBREW_BOTTLES, fetch))
    return found


def pour_prefix(bottles: list[Bottle], official: Official, arch: str, cache: Path, fetch: Fetch,
                root: Path) -> list[Path]:
    """Pour the bundle's bottles and BUILD_ONLY's into root (kept when it holds just these already), a keg
    built from source as its stand-in bottle with the official libraries; returns the prefixes to search:
    root, then each formula's opt dir (keg-only ones are only there)."""
    every = [*bottles, *_build_only(official, arch, bottles[0].built_for or bottles[0].tag, cache, fetch)]
    opts = [root, *(root / "opt" / b.formula for b in every)]
    stamp, poured = root / ".poured", "\n".join([str(root.resolve()), *sorted(b.sha256 for b in every)])
    if stamp.exists() and stamp.read_text() == poured:
        return opts
    if root.exists():
        remove(root)
    for bottle in every:
        keg = brew_prefix.pour(bottle, homebrew.archive(bottle, cache / HOMEBREW_BOTTLES, fetch), root)
        if bottle.built_for:  # its stand-in's libraries are another architecture's
            brew_prefix.use_libraries(keg, official.contents / "Frameworks", arch)
    stamp.write_text(poured)
    print(f"  poured {len(every)} {arch} bottles into {root}", flush=True)
    return opts


def build_wx(official: Official, arch: str, cache: Path, fetch: Fetch, work: Path) -> Path:
    """kicad-mac-builder's wxWidgets, configured and made as its wx.cmake does; returns its wx-config."""
    pin = official.pins.wxwidgets
    sha = macbuilder.commit(pin, official.until, fetch)
    source = macbuilder.git_archive(pin, sha, "wxWidgets", cache / MACOS_SOURCES / "git")
    prefix, tree = work / "wx", work / "wx-src"
    wx_config = prefix / "bin" / "wx-config"
    stamp = prefix / f".built-{sha[:12]}-{arch}"
    if stamp.exists():
        return wx_config
    for d in (prefix, tree):
        if d.exists():
            remove(d)
    tree.mkdir(parents=True)
    with tarfile.open(source) as tar:
        tar.extractall(tree, filter="tar")
    src = top_dir(tree)
    build = macbuilder.wx_build(official.pins.wx_cmake, official.minos, str(prefix))
    tc = _toolchain()
    env = {**build.env, "CC": f"{tc}/clang -arch {arch}", "CXX": f"{tc}/clang++ -arch {arch}",
           "OBJC": f"{tc}/clang -arch {arch}", "OBJCXX": f"{tc}/clang++ -arch {arch}"}
    log = work / "wx.log"
    _run(["./configure", *build.configure], cwd=src, env=env, log=log)
    _run(["make", f"-j{JOBS}", *build.make], cwd=src, env=env, log=log)
    _run(["make", "install", *build.make], cwd=src, env=env, log=log)
    stamp.touch()
    return wx_config


def ngspice_headers(official: Official, cache: Path, fetch: Fetch, work: Path) -> Path:
    """The pinned ngspice source's include dir (KiCad needs ngspice/sharedspice.h)."""
    pin = official.pins.ngspice
    sha = macbuilder.commit(pin, official.until, fetch)
    source = macbuilder.git_archive(pin, sha, "ngspice", cache / MACOS_SOURCES / "git")
    dest = work / "ngspice"
    if not dest.exists():
        with tarfile.open(source) as tar:
            members = [m for m in tar.getmembers() if "/src/include/ngspice/" in m.name]
            tar.extractall(work / "ngspice-src", members=members, filter="tar")
        (work / "ngspice-src").rename(dest)
    return top_dir(dest) / "src" / "include"


def configure_args(src: Path, build: Path, arch: str, official: Official, opts: list[Path], wx_config: Path,
                   ngspice_include: Path) -> list[str]:
    """kicad-mac-builder's CMake arguments for KiCad (CMakeLists.txt, DECLARE_KMB_CMAKE_ARG), pointed at
    the prefix and the official app, and nothing outside them: no other Homebrew."""
    tc = _toolchain()
    py = official.contents / "Frameworks" / "Python.framework"
    version = next(p.name for p in (py / "Versions").iterdir() if p.name[0].isdigit())
    occ = next(o for o in opts if o.name == "opencascade")  # its headers are in include/opencascade
    swig = shutil.which("swig")
    if not swig:
        raise RuntimeError("swig not found: KiCad's CMake needs it (brew install swig)")
    return [
        "cmake", "-S", str(src), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_OSX_ARCHITECTURES={arch}", f"-DCMAKE_OSX_DEPLOYMENT_TARGET={official.minos}",
        f"-DCMAKE_OSX_SYSROOT={_sdk()}",
        f"-DCMAKE_C_COMPILER={tc}/clang", f"-DCMAKE_CXX_COMPILER={tc}/clang++", f"-DCMAKE_OBJCXX_COMPILER={tc}/clang++",
        f"-DCMAKE_PREFIX_PATH={';'.join(map(str, opts))}",
        f"-DCMAKE_IGNORE_PREFIX_PATH={';'.join(p.rstrip('/') for p in HOST_PREFIXES)}",
        "-DCMAKE_FIND_FRAMEWORK=LAST",
        "-DDEFAULT_INSTALL_PATH=/Library/Application Support/kicad",
        "-DKICAD_BUILD_I18N=OFF", "-DKICAD_BUILD_QA_TESTS=OFF", "-DKICAD_SCRIPTING_WXPYTHON=ON",
        f"-DwxWidgets_CONFIG_EXECUTABLE={wx_config}",
        f"-DPYTHON_EXECUTABLE={py}/Versions/{version}/bin/python{version}",
        f"-DPYTHON_INCLUDE_DIR={py}/Versions/{version}/include/python{version}",
        f"-DPYTHON_LIBRARY={py}/Versions/{version}/lib/libpython{version}.dylib",
        f"-DPYTHON_SITE_PACKAGE_PATH={py}/Versions/{version}/lib/python{version}/site-packages",
        f"-DPYTHON_FRAMEWORK={py}",
        f"-DNGSPICE_INCLUDE_DIR={ngspice_include}",
        f"-DNGSPICE_LIBRARY={official.contents}/PlugIns/sim/libngspice.0.dylib",
        f"-DOCC_INCLUDE_DIR={occ}/include/opencascade", f"-DOCC_LIBRARY_DIR={occ}/lib",
        f"-DSWIG_EXECUTABLE={swig}",
    ]


def stale_build(build: Path, src: Path) -> bool:
    """A build dir CMake configured for another source tree (it refuses to reuse one)."""
    cache = build / "CMakeCache.txt"
    if not cache.exists():
        return False
    home = re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$", cache.read_text(), re.M)
    return not home or Path(home.group(1)) != src


def _pkg_config_env(opts: list[Path]) -> dict[str, str]:
    dirs = [str(d) for o in opts for d in (o / "lib" / "pkgconfig", o / "share" / "pkgconfig") if d.is_dir()]
    return {"PKG_CONFIG_LIBDIR": ":".join(dirs), "PKG_CONFIG_PATH": ""}


def build_kicad(src: Path, files: list[str], arch: str, official: Official, bottles: list[Bottle], cache: Path,
                fetch: Fetch, work: Path) -> dict[str, Path]:
    """Build the bundle files (relative to Contents/) that are KiCad's own; returns each one's built file."""
    work.mkdir(parents=True, exist_ok=True)
    src, work = src.resolve(), work.resolve()  # configure scripts want absolute paths
    official = replace(official, contents=official.contents.resolve())
    opts = pour_prefix(bottles, official, arch, cache, fetch, work / "prefix")
    wx_config = build_wx(official, arch, cache, fetch, work)
    build = work / "kicad-build"
    if stale_build(build, src):
        remove(build)
    env = _pkg_config_env(opts)
    log = work / "kicad.log"
    _run(configure_args(src, build, arch, official, opts, wx_config, ngspice_headers(official, cache, fetch, work)),
         env=env, log=log)
    _run(["ninja", "-C", str(build), f"-j{JOBS}", *targets(files)], env=env, log=log)
    built = {}
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        found = [p for p in build.rglob(name) if p.is_file() and not p.is_symlink() and "CMakeFiles" not in p.parts]
        if len(found) != 1:
            raise RuntimeError(f"expected one built {name} under {build}, found {found}")
        built[rel] = found[0]
    return built


# ---- overlaying -------------------------------------------------------------------------------

def relink(built: Path, official: Path) -> None:
    """Give built (a copy, edited in place) the install name, dependencies and rpaths of official."""
    args = []
    if (id_ := macho.install_id(official)) and macho.install_id(built) != id_:
        args += ["-id", id_]
    for old, new in relink_plan(macho.deps(built), macho.deps(official)).items():
        args += ["-change", old, new]
    ours, theirs = macho.rpaths(built), macho.rpaths(official)
    args += [a for r in ours if r not in theirs for a in ("-delete_rpath", r)]
    args += [a for r in theirs if r not in ours for a in ("-add_rpath", r)]
    if args:
        subprocess.run(["install_name_tool", *args, str(built)], check=True, capture_output=True)


def _dyld_info(path: Path, arch: str, section: str) -> list[tuple[str, str | None]]:
    out = subprocess.run(["dyld_info", "-arch", arch, section, str(path)], capture_output=True, text=True, check=True)
    return parse_dyld_info(out.stdout, section)


def check_imports(contents: Path, files: list[Path], arch: str) -> None:
    """Every symbol each file imports from a bundled library is exported by one of its bundled dependencies."""
    resolve = macho.make_resolver(contents)
    exports: dict[Path, set[str]] = {}
    problems = []
    for f in files:
        deps = macho.deps(f)
        system = {dyld_name(d) for d in deps if d.startswith(macho.SYSTEM_PREFIXES)}
        exported = set()
        for d in deps:
            if (path := resolve(d, f)) is not None and not d.startswith(macho.SYSTEM_PREFIXES):
                if path not in exports:
                    exports[path] = {s for s, _ in _dyld_info(path.resolve(), arch, "-exports")}
                exported |= exports[path]
        if missing := missing_imports(_dyld_info(f, arch, "-imports"), system, exported):
            problems.append(f"{f.relative_to(contents)}: {len(missing)} symbols, e.g. {missing[:5]}")
    if problems:
        raise RuntimeError("built binaries import symbols the bundled libraries don't export "
                           "(built against other headers than the official libraries'):\n" + "\n".join(problems))
