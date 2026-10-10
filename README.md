# simee-kicad

simee's fork of KiCad. This branch, `simee-ci` (the default), holds only simee's tooling. The
other branches and tags are KiCad's history, so clone with `--single-branch`.

## What it produces

GitHub releases named `cli-<kicad version>-<n>` (for example `cli-10.0.6-1`), each holding a trimmed
`kicad-cli` from that KiCad release. It contains only what `kicad-cli sch ...`, `fp ...` and `pcb ...`
need: the schematic module, the footprint-assignment one (`sch erc` loads it), the PCB one (every `fp`
and `pcb` command: gerbers, drill, STEP, VRML, SVG, 3D renders, board import), the 3D model plugins (STEP,
VRML, IDF; renders draw the models a board brings with them) and their shared libraries. Up to `cli-10.0.6-3` they are the official, unmodified binaries; a
release built with `--simee-ref simee/<version>` has KiCad's own files built from that branch (see
"Patching KiCad") on the official release's third-party libraries, and its source asset is the
branch's.

| asset | contents |
|---|---|
| `kicad-cli-<v>-macos-arm64.tar.gz`, `-macos-x86_64.tar.gz` | `KiCad.app` with `Contents/MacOS/kicad-cli` (ad hoc signed) |
| `kicad-cli-<v>-windows-x86_64.zip` | `bin\kicad-cli.exe` and its DLLs; needs Windows 10 or later |
| `kicad-cli-<v>-linux-x86_64.tar.gz`, `-linux-arm64.tar.gz` | `bin/kicad-cli` (a wrapper), `libexec/`, and every library but glibc in `lib/`; needs glibc 2.39+ (Ubuntu 24.04, Debian 13). arm64 from `cli-10.0.6-9` |
| `kicad-<v>-source.tar.gz` | the matching KiCad source (GPL-3.0-or-later): the official tag's, or the `simee/<version>` branch's |
| `kicad-cli-<v>-linux-x86_64-sources.tar`, `-linux-arm64-sources.tar` | the exact Debian source of every library in that Linux bundle |
| `kicad-cli-<v>-macos-sources.tar` | the source of every third-party library in the macOS bundles, with Homebrew's formulae and patches |
| `kicad-cli-<v>-windows-x86_64-sources.tar` | the upstream sources of every vcpkg port in the Windows bundle, with the ports (portfiles, patches) |
| `SHA256SUMS` | checksums of everything above |

simee-core pins one release in `cmake/SimeeKicad.cmake` (simee-db's worker image too). simee-agents's
weekly kicad-sync job packages each new stable KiCad release (from its `simee/<version>` branch, below) and
opens the pin-bump PRs. It also rehearses each new major from its release candidates (below).

Run `kicad-cli` with `KICAD_CONFIG_HOME`, `KICAD_DOCUMENTS_HOME` and `KICAD_CACHE_HOME` pointing at
private dirs, or its first run writes into the user's home.

## Building a release

Actions → **package** → run with a KiCad version, or `gh workflow run package.yml -f kicad_version=10.0.6`.
Locally (macOS; the Windows bundle builds anywhere with 7-Zip but only smoke-tests on Windows; the
Linux bundle builds and smoke-tests anywhere with docker):

```bash
uv run kicad-bundle --kicad-version 10.0.6 --platform macos   # -> dist/
uv run pytest
```

The macOS build reads homebrew-core's history from GitHub's API: set `GITHUB_TOKEN` (for example
`GITHUB_TOKEN=$(gh auth token)`), or it runs into the 60 requests an hour allowed without one.

The download cache (`~/.cache/kicad-bundle`, or `--cache`) is pruned after every build: it keeps the
installers of the version just built and of the newest other version, and the source files a
build used in the last 90 days (macOS: the third-party sources, and the UUIDs, formula and SBOM of
each Homebrew bottle tried; only the bottles a `--simee-ref` build poured are kept). The treeless clones of the vcpkg
registries (`vcpkg-registries/`, a few MB) aren't pruned.

How it works: download the official installer (cached in `~/.cache/kicad-bundle`), copy out the app,
walk the shared-library closure of `kicad-cli` + the kifaces it loads (`otool -L` / PE imports): eeschema's,
cvpcb's, which `sch erc` needs for its footprint checks, and pcbnew's, which links opencascade
(`bundle.KIFACES`), and of the 3D model plugins (`bundle.PLUGINS_3D`), which KiCad `dlopen`s from its plugin
folder so no import reaches them. Drop
everything else (on Windows also the app-local Universal CRT, `api-ms-win-*.dll` and `ucrtbase.dll`,
which Windows 10 and later never load), thin and re-sign per architecture on macOS, then smoke-test before
archiving: export the netlist of a known RC filter and run ERC on it, comparing KiCad's nets and ERC
errors; upgrade a KiCad 5 footprint library (`fp upgrade`); export the filter's board as gerbers
(their pads must carry the schematic's nets), drill (its four holes), STEP, VRML and SVG, and render it in 3D,
bare and with each of its STEP and VRML exports as a 3D model on a footprint (`kicad_bundle/smoke/`; see
"Drawing a board").

### Drawing a board

simee-db draws each breakout board on the studio's bench from its own PCB (#20), as the smoke test does
(`smoke.check_drawings`):

```bash
kicad-cli pcb export svg --mode-single --layers F.Cu,F.Mask,F.SilkS,Edge.Cuts --page-size-mode 2 \
  --exclude-drawing-sheet -o board.svg board.kicad_pcb     # flat, the board's area (within 0.1 mm)
kicad-cli pcb render --side top -w 1600 -h 900 -o board.png board.kicad_pcb   # 3D, from the top
```

Both run **headless**, nothing to set up. `pcb render` uses KiCad's CPU raytracer, not OpenGL, and needs
no display: it works on Linux with no `DISPLAY` and no X or Wayland libraries installed (the Linux smoke
test runs it in a bare `ubuntu:24.04` container), and on macOS outside any login session (checked with
`cli-10.0.6-10` from a launchd job in the `Background` session, which has no window server). So no
software-GL fallback is needed; `pcb export svg` remains the cheap 2D one (under a second, where a
`--quality high --floor` render takes about 5 s for a small board). Renders are deterministic: the same PNG,
byte for byte, on macOS arm64 and Linux arm64. KiCad 10.0.6 makes the image up to 32 pixels smaller than
asked (1568 x 872 for the default 1600 x 900, 368 x 168 for 400 x 200), so scale it rather than expect the
exact size.

The bundle has no 3D models (KiCad's library is 3.1 GB), but it has the plugins that load them (STEP, VRML
and IDF; #28), so `pcb render` draws the component bodies of a board that brings its own models: embedded in
the board (KiCad 9+), next to it (`${KIPRJMOD}/...`), or anywhere a path variable set in the environment points
(simee-db's LCSC parts name theirs with `${SIMEE_LCSC_DIR}`). A model it can't find is left out, so a board
whose footprints name KiCad's library (`${KICAD10_3DMODEL_DIR}`) renders without bodies, as do boards imported
from Eagle, which have no models. The smoke test renders the filter board with its own STEP and VRML exports as
a model, half size and lifted over R1, and each render must differ from the bare one (renders are
deterministic). The plugins live where KiCad looks for them: `KiCad.app/Contents/PlugIns/3d` on macOS,
`bin\plugins\3d` on Windows, and on Linux `libexec/plugins/3d`, which the wrapper names to KiCad (below).
They add 0.9 to 1.9 MB unpacked (0.4 to 0.6 MB to each download).

`pcb import` turns another tool's board into a `.kicad_pcb` (Eagle, Altium, CADSTAR, PADS, ...; KiCad
10.0.6's own command, unlike `sch import`): `kicad-cli pcb import --format eagle -o board.kicad_pcb
board.brd`. Upstream's left Eagle boards with fiducials or logos on a restrict layer unloadable;
`simee/10.0.6` fixes that (see "Patching KiCad").

Linux has no official relocatable build, so the Linux bundle comes from the official `kicad/kicad:<v>`
Docker image (Debian, amd64 only): `docker export` its filesystem, walk the ELF `DT_NEEDED` closure of
`kicad-cli` + the kifaces, and copy every library except glibc into `lib/` under the name the loader
asks for. `bin/kicad-cli` sets `LD_LIBRARY_PATH`, `KICAD_STOCK_DATA_HOME` (KiCad otherwise looks for
its data in `/usr/share/kicad`) and `KICAD_STOCK_3D_PLUGINS_HOME` (its 3D plugins, otherwise in
`/usr/lib/<multiarch>/kicad/plugins/3d`; only `simee/<version>` reads it, so a bundle of the official binaries
renders no models and its smoke test doesn't ask it to) and runs `libexec/kicad-cli`. The smoke test runs in a bare `ubuntu:24.04`
container, which proves both the glibc floor and that nothing is missing from `lib/`. The bundle is
larger than the macOS one (about 210 MB, against 90) because eeschema links wx's webview, which pulls in
WebKitGTK. pcbnew's kiface and opencascade add about 30 MB to each bundle (50 on Linux).

The official image is amd64 only, so the arm64 bundle (`--arch arm64`, always with `--simee-ref`; #16) is built
like it rather than repackaged (`linux_build.native`): the Debian image the official one is built `FROM` (its
history names it, `debian:trixie-<date>`), for arm64, with every package the official image has installed from
Debian's archive as it was when that image was made (snapshot.debian.org), then KiCad built from the branch on
it as below and put where the official image has it (`usr/bin`, `usr/lib/aarch64-linux-gnu`), with the official
image's data (`usr/share/kicad/schemas`). The bundle comes from that image as from the official one, and the
build fails unless every Debian source both images have is at the same version. It needs an arm64 docker: the
package workflow's `ubuntu-24.04-arm` runner (free, as this repo is public), or an Apple-silicon Mac, natively:

```bash
uv run kicad-bundle --kicad-version 10.0.6 --platform linux --arch arm64 --simee-ref simee/10.0.6
```

## Rehearsing a new major

A new major KiCad can't be read by the previous one's `kicad-cli` (each bumps the file format), so
kicad-sync gets ready from the major's first release candidate, 5 to 8 weeks before it ships. simee
ships stable releases only: a rehearsal is never published or pinned.

What exists for a release candidate (checked for 10.0.0, October 2026):
- a git tag, `<M>.0.0-rcN` (point releases get RCs too, `10.0.7-rc2`, but change no file format, so
  they aren't rehearsed); GitHub's releases, where the packagers download installers, list stables only;
- official macOS and Windows installers, on KiCad's download server only
  (`kicad-downloads.s3.cern.ch/osx/stable/kicad-unified-universal-10.0.0-rc1.dmg`,
  `.../windows/stable/kicad-10.0.0-rc1-x86_64.exe`). The macOS and Windows packagers take a version with
  no GitHub release from there (`release.py`; a stable's GitHub release can lag its installers by days
  too), dated by the installer's upload (its Last-Modified) instead of the release's publication;
- no `kicad/kicad` Docker image: Docker Hub has no rc tags, and its `nightly` image hasn't been
  updated since February 2026.

So a rehearsal is: simee's patches carried onto the RC tag (pushed as `rehearsal/<rc>`, never
`simee/<version>`), then every packager's source build of KiCad's own files from that branch, with the smoke
and `sch import` fixture tests: macOS and Windows on the RC's own installers, Linux on the newest stable
image (`--base-image kicad/kicad:<stable>`):

```bash
gh workflow run package.yml -f kicad_version=11.0.0-rc1 -f simee_ref=rehearsal/11.0.0-rc1 \
  -f platforms="linux macos windows" -f base_image=kicad/kicad:10.0.7 -f publish=false
```

The stable image's libraries are the ones closest to the RC that exist. When the RC needs a newer one
or another `-dev` package, the build fails: that is what `linux_build.BUILD_DEPS` will need on release day,
when `kicad/kicad:<M>.0.0` exists. On macOS and Windows the RC's toolchain and third-party libraries are
the release's own, so a failure there (an MSVC the runner lacks, a library no Homebrew bottle holds) is
what release day would hit too. A failed rehearsal goes to kicad-sync's repair agent like a failed release, so the fix
lands before the release.

## Licences

The bundles redistribute third-party libraries, several under the LGPL or GPL (cairo, glib, libgit2,
unixODBC, ...), and the combined work is GPL-3.0-or-later. So every release carries the complete
corresponding source of everything it ships rather than a written offer, which would oblige us to
supply sources on request for three years. We ship the source of every bundled library, permissive
ones included, so nothing hinges on classifying each licence correctly.

- Linux: `kicad-bundle` maps each library to the Debian package that owns it (dpkg's records in the
  image), copies that package's `copyright` file into `share/doc/<package>/`, lists them all in
  `THIRD-PARTY.txt`, and downloads each source package at the exact installed version from
  snapshot.debian.org (cached by sha1 under `~/.cache/kicad-bundle/debian-sources`). A library no
  package owns fails the build, unless it is KiCad's own (`libki*`).
- macOS: KiCad's builder (kicad-mac-builder) takes most libraries from Homebrew bottles, relinked and
  re-signed, which keeps each library's Mach-O UUID. `kicad-bundle` maps each library to its formula
  (`homebrew.FORMULAE`), walks the formula's homebrew-core history back from the KiCad release until
  the bottle for the macOS the library was built for holds a file with the same UUID, and takes the
  source archive that bottle's SBOM names (or, in a bottle older than Homebrew's SBOMs, its formula
  names), the patches its formula applies, and the formula itself. Homebrew stopped bottling for Intel
  Macs in September 2026, so KiCad's build machine builds a newer x86_64 keg from source (10.0.7's
  openssl@3 3.6.5): no bottle holds it, but the library names its keg (`Cellar/<formula>/<version>/`, in
  directories compiled in), and the newest bottle of that version for another tag (the arm64 one first)
  stands in for its formula, source and, in a build from a `simee/` branch, headers, with the official
  libraries swapped in (#21).
  wxWidgets (KiCad's fork), ngspice and Python are built by kicad-mac-builder: their pins come from its
  release branch (`10.0` for 10.0.x; master for a release candidate from before that branch was cut) as it
  was when KiCad published the release, a pinned branch
  resolves to its head at that moment, and the version string in the binary must match the pin. Each
  bundle's `THIRD-PARTY.txt` lists file -> component -> where it came from, with the licence files
  of each component's source in `KiCad.app/Contents/Resources/Licenses/<component>/`, and
  `kicad-cli-<v>-macos-sources.tar` holds the sources of both architectures. A library that is none
  of these, nor KiCad's own (`libki*`), fails the build.
- Windows: KiCad builds its DLLs with vcpkg in manifest mode, from `vcpkg.json` (version overrides)
  and `vcpkg-configuration.json` (registries and baselines: microsoft/vcpkg, plus KiCad's own
  kicad-vcpkg-registry for python3, wxwidgets-33, ...) in its source at the release tag. Each DLL
  names the port it was built in (`C:\vcpkg\buildtrees\<port>\...` in its PDB path or `__FILE__`
  strings). `kicad-bundle` resolves each port's version (override, else the registry's baseline),
  reads the port folder from the registry by its git tree (`vcpkg.py`, a treeless shallow clone),
  and evaluates just enough of the portfile to find its downloads (`portfile.py`: URLs, SHA512s,
  patches; downloads behind an `if()` are included anyway, so the sources are a superset). Each
  download must match its SHA512. Checks that this is what KiCad built: a DLL that names its vcpkg
  source folder (`<ref>-<hash>.clean`; the hash covers the archive and its patches) must match a
  download of the port, and otherwise its FileVersion or ProductVersion must start with the port's
  version. `THIRD-PARTY.txt` lists file -> port -> registry and tree, the licence files of each
  port's source go to `share/doc/<port>/`, and `kicad-cli-<v>-windows-x86_64-sources.tar` holds each
  port's downloads and the port folder itself. A DLL that names no port, nor is KiCad's own
  (`kicad-cli.exe`, `_*.dll`, `ki*.dll`) or Microsoft's C++ runtime (`vcruntime140*`, `msvcp140*`,
  ..., checked by its version resource), fails the build. The C++ runtime is shipped as KiCad ships
  it, as Visual Studio 2022 Distributable Code, and the notice says so. Decided in #9: simee keeps
  shipping it rather than requiring the VC++ Redistributable, on George's own Visual Studio licence
  (redistribution is limited to licensed Visual Studio users). Those terms also require whoever
  ships simee to external users to bind them to terms protecting Microsoft's code at least as much.

## Patching KiCad

simee's changes to KiCad live on `simee/<version>` branches cut from the release tag (`simee/10.0.6`),
one commit per change so the rebase onto the next release stays trivial. Prefer backporting an
upstream commit over writing our own, and drop it once the release that has it is tracked.

kicad-sync does that rebase: for a new release it replays the commits of the `simee/<version>` the pins are
built from onto the release tag as the new `simee/<version>`, and opens a PR updating the list below. It
drops a commit when the tag already has its change, or when an upstream commit its message names as
`upstream <sha>` (or a `cherry picked from commit <sha>` line) is in the tag, so name the commit a backport
comes from that way. A conflict goes to its repair agent, which carries the rest on `carry/<version>` for
review; a human then creates `simee/<version>` from it.

`simee/10.0.6` holds:
- `kicad-cli sch import`, backported from KiCad master (upstream 473474c51a3a, in KiCad 11): imports
  Altium, Eagle, CADSTAR, EasyEDA (Std and Pro), LTspice and PADS schematics and saves them as
  `.kicad_sch`, which `sch export netlist` then reads. The output folder must exist.
- an EasyEDA Std import fix: circle net flags (`part_netLabel_Bar`) now connect. Offered upstream as
  https://gitlab.com/kicad/code/kicad/-/merge_requests/2826 (issue kicad#25730, not merged yet; #14);
  keep carrying it until a release we track has it.
- no stray `.kicad_sch` (the virtual root sheet) next to an imported schematic: backport of upstream
  f106b2052cef (in KiCad 11), minus its API save-copy half, which 10.0.x doesn't have (#15).
- a multi-sheet import keeps every sheet: with no project to list several top-level sheets (one per
  Eagle page), `sch import` puts them under a root sheet that the output file holds, so `sch export
  netlist` of the output sees the whole design. Upstream's `sch import` writes the other sheets with
  nothing referencing them; its top-level `import` command, not backported, lists them in a project (#17).
- sheet files land next to the output wherever the input is. The import moved them as Save As does,
  relative to the input's folder, but most importers (Eagle's among them) make them in the project's,
  the output's: an output folder inside the input's got `<out>/<out relative to the input's>/` and a
  root naming files that weren't there. Upstream master has the same bug (#18).
- `kicad-cli pcb import` of an Eagle board leaves out what is on an Eagle layer with no KiCad layer
  (tRestrict, bRestrict, vRestrict, Milling, ...) as the layer mapping dialog does. Its default mapping, the
  one the CLI uses, mapped those layers to `UNSELECTED_LAYER`, which the importer doesn't skip, so a package
  wire on tRestrict (Adafruit draws its fiducials and logos there) was saved on layer `UNDEFINED` and every
  later command failed with "One or more items were found on undefined layers". Upstream master has the
  same bug (#20).
- `KICAD_STOCK_3D_PLUGINS_HOME` names the folder of KiCad's 3D model plugins, as `KICAD_STOCK_DATA_HOME`
  names its data: on Linux KiCad otherwise only looks in the compiled-in absolute
  `/usr/lib/<multiarch>/kicad/plugins/3d` (or under `APPDIR`, with a hard-coded x86_64 triplet), so the
  relocatable bundle couldn't load its plugins (#28).

To build them into a bundle: `uv run kicad-bundle --kicad-version 10.0.6 --platform linux --simee-ref
simee/10.0.6` (the package workflow's `simee_ref` input does the same; it resolves the branch to one
commit for every platform). The Linux packager builds `kicad-cli`, the kifaces and `libki*` from
the branch on the official `kicad/kicad` image itself, with Debian's archive as it was when the image was
made (snapshot.debian.org, from the image's dpkg status time) and every installed package held, then
checks every Debian source it built against has the version the image ships, replaces KiCad's own files
in the repackaged bundle and smoke-tests `sch import` on the fixtures too. It also writes
`kicad-<v>-source.tar.gz` (GitHub's tarball of that commit). The build runs under amd64 emulation on an
arm64 Mac (slow).

macOS (`kicad_bundle/macos_build.py`): the bundle is the official DMG with KiCad's own files rebuilt
from the branch, one architecture at a time (x86_64 cross-built on arm64). They're built against
exactly what the DMG ships, so nothing else changes and its third-party notices and sources still hold:
- the Homebrew bottles its libraries came from (the ones `THIRD-PARTY.txt` lists, found by Mach-O UUID),
  poured into a private prefix (`brew_prefix.py`: each keg relocated as `brew` would; opencascade's among
  them, as pcbnew links it), plus glm's at the release date, which KiCad's CMake needs too. Nothing else
  is searched: no other Homebrew;
- kicad-mac-builder's wxWidgets fork, configured and made with its `wx.cmake` at the pinned commit;
- the DMG's own Python.framework (with its wxPython) and ngspice, and the pinned ngspice's headers;
- kicad-mac-builder's CMake options for KiCad (`DEFAULT_INSTALL_PATH`, `KICAD_SCRIPTING_WXPYTHON`, the
  DMG's deployment target), minus translations and QA tests, which don't change the binaries.

Each built file then gets the install name, dependencies and rpaths of the official file it replaces,
and every symbol it imports from a bundled library must be exported by one, or the build fails. The smoke
test also imports every fixture in `kicad_bundle/smoke/import/` with `sch import`. Needs Xcode 16 or
later (the package workflow selects 16.2, which the official build used), CMake, ninja and swig
(`brew install swig ninja`); both architectures take about an hour, locally on an M-series Mac or
on the `macos-14` runner (free, as this repo is public; 54 minutes for `cli-10.0.6-4`). Locally:

```bash
GITHUB_TOKEN=$(gh auth token) uv run kicad-bundle --kicad-version 10.0.6 --platform macos --simee-ref simee/10.0.6
```

Windows (`kicad_bundle/windows_build.py`): the official installer's bundle with KiCad's own files
(`kicad-cli.exe`, the kifaces `_*.dll`, `ki*.dll`) rebuilt from the branch the way KiCad's builder
(kicad-win-builder's `build.ps1`) builds them: MSVC; vcpkg at the commit `build.ps1` pinned when KiCad
published the installer (kicad-win-builder builds releases and RCs from master), in manifest
mode from the branch's `vcpkg.json` and `vcpkg-configuration.json` (the release tag's, so every port is
the version the official DLLs come from), triplet `x64-windows`; `build.ps1`'s CMake options, minus
translations and Sentry; its swigwin. A built file linked by another MSVC than the official one (the
bundle keeps KiCad's C++ runtime and third-party DLLs), or importing a DLL no official file in the
bundle imports (Windows API sets aside), fails the build. Needs Windows with Visual Studio 2022 and that MSVC (14.44 for
10.0.x): the package workflow's `windows-2022` job, free since this repo is public. vcpkg's builds of the
ports are kept in `~/.cache/kicad-bundle/vcpkg-binaries` (an Actions cache, about 1 GB): the first build
compiles every port (3 hours on the runner), later ones only KiCad (30 minutes).

To try a change on macOS: `dev/build-macos-homebrew.sh <simee/<version> checkout> <build dir> [ninja
target...]` builds `kicad-cli` and the eeschema kiface, or the targets given (`kicad-cli pcbnew_kiface` for
`pcb import`), against Homebrew (a dev build, not a release one), then
`KICAD_CLI=<build dir>/kicad/KiCad.app/Contents/MacOS/kicad-cli uv run pytest` also runs the import
tests, which skip without `KICAD_CLI`. They import each real circuit in `kicad_bundle/smoke/import/`
(Adafruit BME280, SparkFun logic level converter and SparkFun's two-sheet Tsunami Qwiic in Eagle,
Digispark ATtiny85 in Altium, Easy-SDR coax power supply in EasyEDA), export the netlist and compare
it with the source tool's, three times: with the source and output folders apart, and with each inside
the other (`sch_import.LAYOUTS`). For Eagle the expected nets are read straight from the Eagle XML
(`tests/eagle_nets.py`); for the others, checked by hand against the project's own schematic export, as
each `fixture.json` says. An
import that leaves a hidden file next to its output fails too. A fixture whose `fixture.json` has a `board`
(the Adafruit BME280's Eagle `.brd`) is also imported with `pcb import` (`pcb_import.py`): the pads of its
gerbers must carry the nets of the board's signals, read from its XML like the schematic's, and it must draw
(`pcb export svg` the size of its outline, `pcb render`). Each fixture keeps its source's licence.

## AVR toolchain

simee-core's package also ships an AVR toolchain, so the installed `sim_runner` compiles a scenario's
firmware on a machine without one (simee-core finds it at `<app dir>/avr-gcc/bin/avr-gcc`). This repo
builds and publishes it, apart from kicad-cli: GitHub releases named `avr-gcc-<gcc version>-<n>` (for
example `avr-gcc-15.3.0-1`, never marked latest), from Actions → **avr-gcc** (`gh workflow run avr-gcc.yml`;
`-f publish=false` builds and checks only). GitHub won't let a workflow's token tag a commit whose
`.github/workflows/` differs from `simee-ci`'s head (HTTP 403, "Resource not accessible by integration"; inferred,
#23: `cli-10.0.6-9` tagged a commit that changes `package.yml`, while the avr-gcc run that built
`avr-gcc-15.3.0-1` couldn't tag its commit after `simee-ci` changed `package.yml` during the build, so that
release was published by hand from the run's artifacts). So when `simee-ci` changes a workflow during a
build, both workflows' release jobs (`kicad_bundle/tag_target.py`) tag its head instead, if what the release
is built from (the Python packages, `pyproject.toml`, `uv.lock` and the workflow itself) is unchanged
there, and otherwise fail, saying so: dispatch the workflow again.

| asset | contents |
|---|---|
| `avr-gcc-<v>-macos-arm64.tar.gz`, `-macos-x86_64.tar.gz` | the toolchain root; macOS 11 or later |
| `avr-gcc-<v>-linux-x86_64.tar.gz` | the toolchain root; glibc 2.36 or later (Debian 12, Ubuntu 22.10) |
| `avr-gcc-<v>-windows-x86_64.zip` | the toolchain root, `.exe`s; Windows 10 or later |
| `avr-gcc-<v>-source.tar` | every upstream source archive it is built from, and the scripts that build it (`simee-build/`) |
| `avr-gcc-<v>-runtime-sources.tar` | the exact Debian sources of the C/C++ runtime the Linux and Windows programs link statically |
| `SHA256SUMS` | checksums of everything above, also in the release notes for simee-core's pin |

Each archive has one top folder, the toolchain root: `bin/avr-gcc`, `bin/avr-objcopy` and the rest of
binutils, `avr/include` and `avr/lib` (avr-libc), `lib/gcc/avr/<v>/`, `libexec/gcc/avr/<v>/` (cc1, cc1plus,
lto1, the LTO plugin), `share/doc/<component>/` (licences) and `THIRD-PARTY.txt`. GCC finds its own
programs, binutils and avr-libc relative to `bin/avr-gcc`, so the root runs wherever it is copied: no
`--with-as`/`--with-ld` (which would compile in absolute paths), GMP, MPFR and MPC built in GCC's tree and
linked statically, no zstd or isl, and on Linux and Windows the C/C++ runtime linked statically too
(`-static-libstdc++ -static-libgcc`; `-static`). Every build is checked that way (`avr_toolchain/check.py`):
with its build folder moved away, the archive is unpacked into a temp dir with a space in its path, and
with an empty `PATH` it compiles simee-core's `fixtures/blink/blink.c` for the ATmega328P as C, as C++
and with `-flto`, and `avr-objcopy` makes an Intel HEX of it. The Windows archive is checked on the
`windows-2022` runner, the x86_64 macOS one under Rosetta.

One upstream limit: on Windows, `-flto` fails (`collect2.exe: fatal error: CreateProcess: No such file or
directory`) when the toolchain's path has a space, e.g. under `C:\Program Files`. The driver hands collect2
lto-wrapper's path with each space escaped by a backslash (`C:/with\ space/...`), which Windows can't run.
Plain compiles work from any path, and simee-core doesn't use `-flto`; the check compiles the Windows LTO
variant from a copy in a path without spaces.

Which build: simee builds GCC 15.3.0, binutils 2.47 and avr-libc 2.3.2 from GNU's and avrdudes' release
archives, unmodified (pins and SHA256s in `avr_toolchain/components.py`), rather than repackaging
Arduino's `avr-gcc 7.3.0-atmel3.6.1-arduino7` (what simee-core's tests used on the Mac host) or
Microchip's toolchain. Arduino's is built for macOS x86_64 only (Rosetta on Apple silicon) and for 32-bit
Windows (`i686-w64-mingw32`), from Atmel's patched GCC 7 with Arduino's scripts, so its exact
corresponding source would have to be reassembled from someone else's archives; and GCC 7 has been out of
support since 2019. Microchip's is a binary download with the same source question. Building from
upstream gives a native arm64 build, a supported compiler (and avr-libc 2.3's newer devices), and a source
asset that is simply the archives built from. `-Os` code differs a little from GCC 7's: simee-core's
firmware fixtures and tests pin the behaviour that matters.

How (`avr_toolchain/build.py`): configure, make and `make install-strip` binutils, then GCC (C and C++,
`--with-avrlibc`), then configure avr-libc with `--host=avr` and build it with the new compiler. A host the
build machine can't run natively is cross-built (a "Canadian cross") with the build machine's own AVR
toolchain building the target libraries, so each job builds its native host first: the `macos-14`
(arm64) job builds macos-arm64 then macos-x86_64 (`clang -arch x86_64`); the Linux job, in a `debian:12`
container for its glibc 2.36, builds linux-x86_64 then windows-x86_64 with Debian's mingw-w64 (win32
threads). On the runners the Linux job takes about an hour, the macOS one two. The AVR libraries' debug
info is stripped (`avr-strip --strip-debug`), which takes a toolchain from about 600 MB to 350 MB unpacked
(75 to 100 MB archived). Locally:

```bash
uv run avr-toolchain build --host macos-arm64     # -> dist/, checked
uv run avr-toolchain build --host macos-x86_64    # cross-built with the arm64 one in work/
AVR_TOOLCHAIN=dist/avr-gcc-15.3.0-macos-arm64.tar.gz uv run pytest tests/test_avr_toolchain.py
```

Licences: binutils and GCC are GPL-3.0-or-later, libgcc (and libstdc++, which isn't built for the AVR)
under the GCC Runtime Library Exception, so firmware compiled with it carries no GPL obligation; GMP, MPFR
and MPC, linked into the compiler, are LGPL-3.0-or-later; avr-libc, linked into firmware, is BSD-3-Clause.
Each archive has the licence files of every component's source in `share/doc/<component>/` and a
`THIRD-PARTY.txt` naming them, their upstream URL and SHA256. As for kicad-cli, every release carries the
complete corresponding source rather than a written offer: the upstream archives with the scripts that
built them, and for the Linux and Windows builds the exact Debian sources (snapshot.debian.org, with each
package's Built-Using: gcc-mingw-w64's libstdc++ is gcc-12's) of the runtime linked in statically, whose
copyright files go to `share/doc/<package>/`. On macOS the programs link only the OS's own libraries
(libSystem, libc++, libz, libiconv).

## Arm toolchain

simee-core compiles RP2040 and RP2350 (Arm) firmware with Arm's own GNU toolchain release for
arm-none-eabi (simee-core#107, #111), and ships it in its package as it ships avr-gcc. This repo publishes it trimmed, with its
sources: GitHub releases named `arm-gcc-<Arm release>-<n>` (for example `arm-gcc-15.2.rel1-1`, never marked
latest), from Actions → **arm-gcc** (`gh workflow run arm-gcc.yml`; `-f publish=false` packages and checks
only). The release is pinned in `arm_toolchain/components.py`, with the SHA256 Arm publishes next to each
download.

| asset | contents |
|---|---|
| `arm-gcc-<v>-macos-arm64.tar.xz` | the toolchain root |
| `arm-gcc-<v>-macos-x86_64.tar.xz` | the toolchain root, its host programs built by simee; macOS 11 or later |
| `arm-gcc-<v>-linux-x86_64.tar.xz`, `-linux-arm64.tar.xz` | the toolchain root |
| `arm-gcc-<v>-windows-x86_64.zip` | the toolchain root, `.exe`s |
| `arm-gnu-toolchain-src-snapshot-<v>.tar.xz` | Arm's source snapshot for the release, unchanged |
| `arm-gcc-<v>-build-scripts.tar` | the scripts that build the macOS x86_64 programs from it (`simee-build/`) |
| `SHA256SUMS` | checksums of everything above, also in the release notes for simee-core's pin |

Arm's binaries aren't rebuilt: `arm_toolchain/package.py` copies out of each of Arm's archives (about 1 GB
unpacked) only what an RP2040 (Cortex-M0+, no FPU) or RP2350 (Cortex-M33 in Arm mode) build runs and links,
`package.KEEP`: the C and C++ drivers, binutils, `cc1`, `cc1plus`, `collect2` and LTO, the headers, and newlib,
libstdc++ and libgcc for the `thumb/v6-m/nofp` and `thumb/v8-m.main+fp/softfp` multilibs
(`components.MULTILIBS`, about 20 MB each), plus Arm's build manifest. That is about 205 MB unpacked, 38 MB as
`.tar.xz` (from `arm-gcc-15.2.rel1-3`; the RP2040's multilib alone, up to `-2`, 186 and 36). Each archive
has one top folder, the toolchain root (`bin/arm-none-eabi-gcc`), which runs from wherever it is copied, like
Arm's own. Arm's Windows zip has no top folder; ours does, like the others. Every archive is checked
(`arm_toolchain/check.py`) on the OS it is for, by the workflow's runners: unpacked into a temp dir with a
space in its path, with an empty `PATH`, for each multilib the C driver must pick it for pico-sdk's flags for
its chip (`thumb/v6-m/nofp` for `-mcpu=cortex-m0plus -mthumb`, `thumb/v8-m.main+fp/softfp` for `-mcpu=cortex-m33
-mthumb -march=armv8-m.main+fp+dsp -mfloat-abi=softfp -mcmse`), and with those flags it compiles and links a C
and a C++ program (`arm_toolchain/smoke/`, with newlib's `nosys.specs`) into Arm ELF files that
`arm-none-eabi-objcopy` turns into raw images. Locally:

```bash
uv run arm-toolchain package --host macos-arm64    # -> dist/, checked when it runs here
uv run arm-toolchain sources                       # -> dist/<Arm's source snapshot>
ARM_TOOLCHAIN=dist/arm-gcc-15.2.rel1-macos-arm64.tar.xz uv run pytest tests/test_arm_toolchain.py
```

Arm builds no macOS x86_64 toolchain after 14.2.rel1, so simee builds that one (`arm_toolchain/build.py`, #26)
and every host still gets the same release. The target side doesn't depend on the host: the headers,
newlib, libstdc++, libgcc and the specs (everything `package.KEEP` keeps outside `bin/`, `libexec/` and
`arm-none-eabi/bin/`) come unchanged from Arm's macOS arm64 archive. Only the host programs are built, from
Arm's source snapshot: binutils from its `binutils-gdb`, and GCC's `all-gcc` and `all-lto-plugin` (the
drivers, cc1, cc1plus, lto1, collect2, lto-wrapper, liblto_plugin) from its `gcc`, with the snapshot's GMP,
MPFR, MPC and isl in GCC's tree, linked statically. They take Arm's own configure options, read from the
`*-manifest.txt` in its arm64 archive at build time (`--with-multilib-list=aprofile,rmprofile`,
`--with-native-system-header-dir=/include`, `--enable-checking=release`, ...), with only the folders, the
bug URL, the languages (C and C++: Arm's Fortran isn't kept) and the pkgversion (`Arm GNU Toolchain
15.2.rel1, built by simee for macOS x86_64`) replaced, and they are built against Arm's sysroot, so the
driver picks the same multilibs and searches the same paths. The sysroot is under the install prefix, which
GCC and ld then find relative to themselves wherever the root is copied; the build deletes that prefix once
the programs are copied out, so the check can't reach into it, and fails if any program Arm's arm64 archive
keeps wasn't built. On an arm64 Mac (the workflow's `macos-14` job) they are cross-built with `clang -arch
x86_64` for macOS 11 and checked under Rosetta; on an Intel Mac they build natively. Cross-building, GCC's
build runs an `arm-none-eabi-gcc` for the build machine (`-dumpspecs`): Arm's own arm64 one, from the same
archive. Arm's snapshot is a git checkout without the generated `.info` manuals, so make runs with
`MAKEINFO=true` and makes none (none are kept). Like Arm's, the programs link only macOS's own libraries
(libSystem, libc++, libiconv), and no zstd; but the OS's libz rather than the bundled zlib, which doesn't
compile against current macOS SDKs (as for avr-gcc). Checked against Arm's arm64 build on an M-series Mac
(#26): the same files, the same `-print-multi-lib` (39 multilibs), `-print-search-dirs` and
`-print-sysroot`, byte-identical raw images of the smoke programs (with and without `-flto`), and identical
`-O2` Cortex-M0+ assembly for the first 291 of GCC's `gcc.c-torture/execute` tests; 167 MB unpacked, 37 MB
as `.tar.xz` (with the RP2040's multilib only; the RP2350's adds about 20 MB, 2 compressed). Locally:

```bash
uv run arm-toolchain package --host macos-x86_64   # -> dist/, checked (under Rosetta on arm64)
```

Licences: binutils and GCC are GPL-3.0-or-later, libgcc and libstdc++ under the GCC Runtime Library
Exception, so firmware linked with them carries no GPL obligation; newlib's libc, libm and libnosys are under
the BSD-style licences its `COPYING.NEWLIB` lists; GMP, MPFR and MPC (LGPL-3.0-or-later), isl (MIT) and
libiconv (LGPL-2.1-or-later) are linked into the compilers; the Windows programs also hold MinGW-w64's runtime.
Each archive has the licence files of each of those components from Arm's source snapshot in
`share/doc/<folder>/` (`components.SHIPPED`), and a `THIRD-PARTY.txt` naming Arm's download, the trim and the
components. As for kicad-cli and avr-gcc, every release carries the complete corresponding source rather than
a written offer: Arm's snapshot, which every component above is built from, and for the macOS x86_64
programs, which are simee's build, the scripts that built them (`arm-gcc-<v>-build-scripts.tar`, as avr-gcc's
`simee-build/`).
