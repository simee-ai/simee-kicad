"""Linux: build KiCad's own binaries from a simee/<version> branch and lay them over the official bundle.

The official kicad/kicad image is plain Debian trixie with Debian's libraries (KiCad's kicad-docker
Dockerfile.<x>-stable). So the branch is built on that very image, with Debian's archive as it was
when the image was made (snapshot.debian.org) and every installed package held: the -dev packages
are then those of the libraries the bundle ships, and the result links against exactly them. Only
KiCad's own files (kicad-cli, the kifaces, libki*) are replaced; the third-party closure,
its notices and its sources stay as linux.assemble made them.

An architecture the official image doesn't exist for (arm64: it is amd64 only) gets a native image
instead (native): the Debian image the official one was built FROM, for that architecture, with the
official image's packages installed from the same day's archive, and KiCad built from the branch on it
put where the official image has it. linux.assemble then takes the bundle from that image.
"""

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kicad_bundle import bundle, debian
from kicad_bundle.bundle import KIFACES, PLUGINS_3D
from kicad_bundle.elf import ARCHES, Arch

# kicad-docker's build dependencies, less what only its QA run or library installs need.
BUILD_DEPS = (
    "build-essential cmake ninja-build pkg-config gettext swig protobuf-compiler libbz2-dev libcairo2-dev "
    "libglu1-mesa-dev libgl1-mesa-dev libglew-dev libx11-dev libwxgtk3.2-dev libwxgtk-webview3.2-dev "
    "mesa-common-dev python3-dev python3-wxgtk4.0 libboost-all-dev libglm-dev libcurl4-openssl-dev "
    "libgtk-3-dev libngspice0-dev ngspice-dev libocct-modeling-algorithms-dev libocct-modeling-data-dev "
    "libocct-data-exchange-dev libocct-visualization-dev libocct-foundation-dev libocct-ocaf-dev "
    "unixodbc-dev zlib1g-dev shared-mime-info libgit2-dev libsecret-1-dev libnng-dev libprotobuf-dev "
    "libzstd-dev libspnav-dev libpoppler-glib-dev"
)
# kicad-docker's cmake options (KICAD_BUILD_I18N only adds translations, which the bundle leaves out).
CMAKE_FLAGS = ("-G Ninja -DCMAKE_BUILD_TYPE=Release -DKICAD_SCRIPTING_WXPYTHON=ON -DKICAD_USE_OCC=ON "
               "-DKICAD_SPICE=ON -DKICAD_BUILD_I18N=OFF -DCMAKE_INSTALL_PREFIX=/usr -DKICAD_USE_CMAKE_FINDPROTOBUF=ON")
# The bundle's KiCad files: libexec/<binary>, PLUGINS_DIR/<plugin> and lib/<library>.
BINARIES = ("kicad-cli", *(f"_{k}.kiface" for k in KIFACES))
PLUGINS = tuple(f"libs3d_plugin_{p}.so" for p in PLUGINS_3D)
# Where the bundle keeps the 3D plugins; its wrapper names it to KiCad (KICAD_STOCK_3D_PLUGINS_HOME, a
# simee/<version> change: KiCad otherwise looks in the absolute /usr/lib/<multiarch>/kicad/plugins/3d).
PLUGINS_DIR = "libexec/plugins/3d"
NINJA_TARGETS = " ".join(("kicad-cli", *(f"{k}_kiface" for k in KIFACES), *(f"s3d_plugin_{p}" for p in PLUGINS_3D)))
KICAD_LIBS = "libki*"
SOURCES = "dpkg-sources.txt"


def snapshot_stamp(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _snapshot(stamp: str, scheme: str = "https") -> str:
    """A RUN step's start: apt's sources are Debian's archive as it was at stamp (scheme http where the image
    has no CA certificates yet: apt checks the archive's signatures either way)."""
    snap = f"deb [check-valid-until=no] {scheme}://snapshot.debian.org/archive"
    return f"""RUN rm -f /etc/apt/sources.list.d/* && \\
    printf '%s\\n' '{snap}/debian/{stamp}/ trixie main' \\
                   '{snap}/debian/{stamp}/ trixie-updates main' \\
                   '{snap}/debian-security/{stamp}/ trixie-security main' > /etc/apt/sources.list && \\"""


def dockerfile(image: str, stamp: str) -> str:
    """Build kicad-cli, the kifaces and the 3D plugins (with the libki* they need) from kicad-src.tar.gz into
    /out, on image, with Debian's archive at stamp and the image's packages held."""
    return f"""FROM {image}
USER root
{_snapshot(stamp)}
    apt-mark hold $(dpkg-query -W -f '${{Package}}\\n') > /dev/null && \\
    apt-get update && \\
    apt-get install -y --no-install-recommends {BUILD_DEPS}
COPY kicad-src.tar.gz /src/
RUN mkdir -p /src/kicad/build && tar -xzf /src/kicad-src.tar.gz -C /src/kicad --strip-components=1
WORKDIR /src/kicad/build
RUN cmake {CMAKE_FLAGS} .. && ninja {NINJA_TARGETS}
RUN mkdir /out && \\
    find . \\( {" -o ".join(f"-name {b}" for b in (*BINARIES, *PLUGINS))} -o -name '{KICAD_LIBS}.so.*' \\) -type f -exec cp {{}} /out/ \\; && \\
    strip --strip-unneeded /out/* && \\
    dpkg-query -W -f '${{Package}}\\t${{source:Package}}\\t${{source:Version}}\\n' > /out/{SOURCES}
"""


def _sources(rows: str) -> dict[str, tuple[str, str]]:
    return {pkg: (src, ver) for pkg, src, ver in (line.split("\t") for line in rows.splitlines() if line)}


def mismatched_sources(image: dict[str, tuple[str, str]], build: dict[str, tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(source, image version, build version) for every Debian source both have at different versions:
    a -dev package from another version than the library the bundle ships."""
    shipped = dict(image.values())
    return sorted({(src, shipped[src], ver) for src, ver in build.values() if src in shipped and shipped[src] != ver})


def _unversioned(name: str) -> str:
    return name.split(".so", 1)[0]


def overlay(root: Path, built: Path) -> list[str]:
    """Replace the bundle's KiCad files (libexec/<binary>, the 3D plugins, lib/libki*) with those in built; returns the
    replaced paths. Refuses to leave any official KiCad file in place. A build of another version than the
    image's (a release-candidate rehearsal) names KiCad's libraries by its own version
    (libkicommon.so.10.0.7 for libkicommon.so.10.0.6): those replace the image's."""
    by_stem = {_unversioned(p.name): p.name for p in built.glob(f"{KICAD_LIBS}.so*")}
    targets = [*(f"libexec/{b}" for b in BINARIES), *(f"{PLUGINS_DIR}/{p}" for p in PLUGINS)]
    for official in sorted((root / "lib").glob(KICAD_LIBS)):
        if official.is_symlink():
            continue
        new = by_stem.get(_unversioned(official.name), official.name)
        if new != official.name and (built / new).is_file():
            official.unlink()
        targets.append(f"lib/{new}")
    return bundle.overlay(root, targets, built)


def plugins_dir(arch: Arch) -> str:
    """Where KiCad's image keeps its 3D plugins (KICAD_PLUGINDIR/kicad/plugins/3d)."""
    return f"usr/lib/{arch.multiarch}/kicad/plugins/3d"


def _docker_build(context: Path, tag: str, arch: Arch) -> None:
    subprocess.run(["docker", "build", "--platform", arch.docker, "-t", tag, str(context)], check=True)


def _fresh(*dirs: Path) -> None:
    for d in dirs:
        if d.exists():
            shutil.rmtree(d)


def build(image: str, rootfs: Path, src: Path, work: Path, arch: Arch = ARCHES["x86_64"]) -> Path:
    """Build the branch in src (a source tarball) on image (for arch; rootfs: the official image's exported
    filesystem); returns the folder holding the binaries. Fails if a -dev package isn't the version of the
    official image's library."""
    context, out = work / f"linux-{arch.name}-build", work / f"linux-{arch.name}-built"
    _fresh(context, out)
    context.mkdir(parents=True)
    stamp = snapshot_stamp((rootfs / debian.DPKG_STATUS).stat().st_mtime)
    (context / "Dockerfile").write_text(dockerfile(image, stamp))
    shutil.copy2(src, context / "kicad-src.tar.gz")
    tag = f"simee-kicad-linux-{arch.name}-build"
    _docker_build(context, tag, arch)
    container = subprocess.run(["docker", "create", "--platform", arch.docker, tag],
                               capture_output=True, text=True, check=True).stdout.strip()
    try:
        subprocess.run(["docker", "cp", f"{container}:/out", str(out)], check=True)
    finally:
        subprocess.run(["docker", "rm", container], check=False, capture_output=True)
    check_sources(rootfs, _sources((out / SOURCES).read_text()), "built against")
    return out


def image_sources(rootfs: Path) -> dict[str, tuple[str, str]]:
    """package -> (source, source version) of every package installed in an exported image."""
    return {p.name: (p.source, p.source_version)
            for p in debian.packages((rootfs / debian.DPKG_STATUS).read_text()).values()}


def check_sources(rootfs: Path, other: dict[str, tuple[str, str]], what: str) -> None:
    """Fail unless every Debian source in other that the image in rootfs has is at the image's version."""
    if bad := mismatched_sources(image_sources(rootfs), other):
        raise RuntimeError(f"{what} other library versions than the official image ships: {bad}")


DEBUERREOTYPE = re.compile(r"debian\.sh (.*?)out/ '([^']+)' '@(\d+)'")


def debian_base(history: list[str]) -> str:
    """The dated Debian image (debian:<suite>-<YYYYMMDD>[-slim]) an image was built FROM, from its
    history (`docker image history`): debuerreotype, which builds Debian's images, records its command."""
    for line in history:
        if m := DEBUERREOTYPE.search(line):
            day = datetime.fromtimestamp(int(m.group(3)), timezone.utc).strftime("%Y%m%d")
            return f"debian:{m.group(2)}-{day}{'-slim' if '--slim' in m.group(1) else ''}"
    raise RuntimeError("the image's history names no Debian image it was built from")


def _history(image: str, arch: Arch) -> list[str]:
    subprocess.run(["docker", "pull", "--quiet", "--platform", arch.docker, image], check=True, capture_output=True)
    return subprocess.run(["docker", "image", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def runtime_dockerfile(base: str, stamp: str, packages: list[str]) -> str:
    """base with packages installed from Debian's archive at stamp: those Debian has for the image's
    architecture (the official image's amd64-only ones, such as Intel's VA drivers, are left out and named)."""
    return f"""FROM {base}
{_snapshot(stamp, "http")}
    apt-get update && \\
    apt-cache dumpavail | sed -n 's/^Package: //p' | sort -u > /tmp/available && \\
    printf '%s\\n' {" ".join(sorted(packages))} | sort > /tmp/wanted && \\
    comm -23 /tmp/wanted /tmp/available | sed 's/^/not for this architecture: /' && \\
    apt-get install -y --no-install-recommends $(comm -12 /tmp/wanted /tmp/available) && \\
    rm /tmp/available /tmp/wanted
"""


def native_context(context: Path, runtime: str, built: Path, rootfs: Path, arch: Arch, data: tuple[str, ...]) -> None:
    """A docker build context adding KiCad's files in built to the runtime image where the official image
    has them (usr/bin, the multiarch lib dir), with the official image's data dirs (in rootfs)."""
    (context / "usr/bin").mkdir(parents=True)
    (context / "usr/lib" / arch.multiarch).mkdir(parents=True)
    for name in BINARIES:
        shutil.copy2(built / name, context / "usr/bin" / name)
    for lib in built.glob(f"{KICAD_LIBS}.so*"):
        shutil.copy2(lib, context / "usr/lib" / arch.multiarch / lib.name)
    (plugins := context / plugins_dir(arch)).mkdir(parents=True)
    for name in PLUGINS:
        shutil.copy2(built / name, plugins / name)
    for d in data:
        shutil.copytree(rootfs / d, context / d, symlinks=True)
    (context / "Dockerfile").write_text(f"FROM {runtime}\nCOPY usr/ /usr/\n")


def native(image: str, rootfs: Path, src: Path, work: Path, arch: Arch, data: tuple[str, ...]) -> str:
    """A local image for arch laid out like the official image (rootfs: its export; image: its name),
    with KiCad built from src: the official image's Debian base for arch, its packages from the same
    day's archive, KiCad's files and its data dirs. Returns the image's tag."""
    base = debian_base(_history(image, ARCHES["x86_64"]))
    if (ours := debian_base(_history(base, arch))) != base:
        raise RuntimeError(f"{base} for {arch.name} was built from another Debian ({ours})")
    stamp = snapshot_stamp((rootfs / debian.DPKG_STATUS).stat().st_mtime)
    runtime, final = f"simee-kicad-linux-{arch.name}-runtime", f"simee-kicad-linux-{arch.name}"
    context, native_ctx = work / f"linux-{arch.name}-runtime", work / f"linux-{arch.name}-native"
    _fresh(context, native_ctx)
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text(runtime_dockerfile(base, stamp, sorted(image_sources(rootfs))))
    print(f"  {runtime}: {base} with the official image's packages from Debian's archive at {stamp}")
    _docker_build(context, runtime, arch)
    built = build(runtime, rootfs, src, work, arch)
    native_context(native_ctx, runtime, built, rootfs, arch, data)
    _docker_build(native_ctx, final, arch)
    return final

