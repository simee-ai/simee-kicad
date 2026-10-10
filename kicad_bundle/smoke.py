"""Prove a bundle works: export the netlist of a known RC filter and compare KiCad's nets, run KiCad's
electrical rules check on it and compare the errors, then run what pcbnew does (upgrade a footprint
library; export the filter's board as gerbers, drill, STEP, VRML and SVG, render it in 3D, with and without
3D models made of it) and check each output."""

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

SCHEMATIC = Path(__file__).parent / "smoke" / "rc_filter.kicad_sch"
EXPECTED = [["C1.1"], ["C1.2", "R1.2"], ["R1.1"]]
# ERC's errors (type, first item) on it: nothing drives its GND symbol's power input. Its warnings depend
# on the symbol libraries installed, which the bundle has none of.
EXPECTED_ERC = [("power_pin_not_driven", "Symbol #PWR01 Pin 1 [Power input, Line]")]
# The filter's board: its copper pads carry the schematic's nets (EXPECTED), each through a plated hole.
BOARD = SCHEMATIC.with_suffix(".kicad_pcb")
EXPECTED_HOLES = 4
BOARD_SIZE = (20.0, 10.0)  # its outline, mm
BOARD_CENTRE = (110.0, 100.0)  # of its outline: the origin of the models made of it, mm
# How simee-db draws a board (simee UI.md 5.7): its top copper, mask, silkscreen and outline in one flat SVG
# of the board's area, and a 3D view from the top. KiCad's SVG of the board area is a little off its outline
# (19.9898 x 9.9822 mm for the filter), and its render up to 32 pixels smaller than asked (368 x 168 for
# 400 x 200, 1568 x 872 for the default 1600 x 900; the same on every platform and run).
SVG_LAYERS = "F.Cu,F.Mask,F.SilkS,Edge.Cuts"
SVG_TOLERANCE = 0.1
RENDER_SIZE = (400, 200)
RENDER_SHORTFALL = 32
# A 3D model on a footprint, as a board that brings its own models refers to it (next to the board): a
# smoke board model, half size and 3 mm up, so it covers part of the board in a top view.
MODEL = '(model "{path}" (offset (xyz 0 0 3)) (scale (xyz 0.5 0.5 0.5)) (rotate (xyz 0 0 0)))'
# A footprint library in KiCad 5's format, which `fp upgrade` rewrites in the current one.
FOOTPRINTS = SCHEMATIC.parent / "footprints.pretty"
EXPECTED_FOOTPRINTS = ["R_Axial_P5.08mm"]
# Where KiCad writes config, documents and caches; point them at a private dir.
KICAD_HOMES = ("KICAD_CONFIG_HOME", "KICAD_DOCUMENTS_HOME", "KICAD_CACHE_HOME")
UNESCAPE = re.compile(r"\\(.)")
TOKEN = re.compile(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()]+')
# A gerber X2 pad: its pad attribute (ref, pin[, function]) then its net attribute.
GERBER_PAD = re.compile(r"%TO\.P,([^,*]+),([^,*]+)[^*]*\*%\s*%TO\.N,([^*]*)\*%")
DRILL_HOLE = re.compile(r"^X-?[\d.]+Y-?[\d.]+$", re.M)


def parse_sexpr(text: str) -> list:
    stack: list[list] = [[]]
    for tok in TOKEN.findall(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(UNESCAPE.sub(r"\1", tok[1:-1]) if tok.startswith('"') else tok)
    return stack[0][0]


def _children(node: list, head: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def netlist_nets(text: str) -> list[list[str]]:
    """Nets from a kicadsexpr netlist as sorted "REF.PIN" lists."""
    nets = []
    for net in _children(_children(parse_sexpr(text), "nets")[0], "net"):
        pins = sorted(f"{_children(n, 'ref')[0][1]}.{_children(n, 'pin')[0][1]}" for n in _children(net, "node"))
        if pins:
            nets.append(pins)
    return sorted(nets)


def netlist_components(text: str) -> dict[str, str]:
    """Components (ref -> value) of a kicadsexpr netlist; KiCad leaves power symbols out."""
    comps = _children(parse_sexpr(text), "components")
    return {_children(c, "ref")[0][1]: _children(c, "value")[0][1]
            for c in (_children(comps[0], "comp") if comps else [])}


def kicad_env(home: Path) -> dict[str, str]:
    """Keep KiCad's config, document and cache dirs out of the user's home."""
    return {**os.environ, **{k: str(home / k) for k in KICAD_HOMES}}


def erc_errors(report: str) -> list[tuple[str, str]]:
    """(type, first item's description) of each error in a kicad-cli JSON ERC report, sorted."""
    return sorted((v["type"], v["items"][0]["description"] if v.get("items") else "")
                  for sheet in json.loads(report)["sheets"] for v in sheet["violations"] if v["severity"] == "error")


def gerber_nets(gerbers: list[str]) -> list[list[str]]:
    """Nets as sorted "REF.PIN" lists, from the pad attributes of gerber X2 copper layers. KiCad names a
    pad on no net N/C."""
    nets = defaultdict(set)
    for text in gerbers:
        for ref, pin, net in GERBER_PAD.findall(text):
            if net != "N/C":
                nets[net].add(f"{ref}.{pin}")
    return sorted(sorted(pins) for pins in nets.values())


def copper_nets(gerbers: dict[str, str]) -> list[list[str]]:
    """gerber_nets of the copper layers among gerbers (file name -> text)."""
    return gerber_nets([t for t in gerbers.values() if "%TF.FileFunction,Copper," in t])


def drill_holes(excellon: str) -> int:
    return len(DRILL_HOLE.findall(excellon))


def svg_size(svg: str) -> tuple[float, float]:
    """An SVG's width and height in mm."""
    root = ET.fromstring(svg.encode())
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise RuntimeError(f"not an SVG: {root.tag}")
    return tuple(float(root.get(k).removesuffix("mm")) for k in ("width", "height"))


def png_size(data: bytes) -> tuple[int, int]:
    """A PNG's width and height, from its header."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RuntimeError("not a PNG")
    return struct.unpack(">II", data[16:24])


def export_gerbers(cli: list[str], board: Path, folder: Path) -> dict[str, str]:
    """board's gerbers, written to folder: file name -> text."""
    run(cli, ["pcb", "export", "gerbers", "-o", f"{folder}/", str(board)], folder)
    return {p.name: p.read_text() for p in folder.iterdir()}


def run(cli: list[str], args: list[str], out: Path) -> Path:
    """Run `cli args`, which must write out (a file or folder) without logging errors; returns out."""
    result = subprocess.run([*cli, *args], env=kicad_env(out.parent), capture_output=True, text=True)
    if result.returncode != 0 or not out.exists():
        raise RuntimeError(f"kicad-cli {' '.join(args[:2])} failed ({result.returncode}): "
                           f"{(result.stdout + result.stderr).strip()[-2000:]}")
    # KiCad logs some problems (missing data files, libraries) as errors yet still exports.
    errors = [line for line in result.stderr.splitlines() if "Error:" in line]
    if errors:
        raise RuntimeError(f"kicad-cli {' '.join(args[:2])} reported errors:\n" + "\n".join(errors))
    return out


def _check_pcbnew(cli: list[str], tmp: Path, models: bool = True) -> None:
    """Raise unless pcbnew's commands work: `fp upgrade` rewrites FOOTPRINTS in the current format, and
    BOARD's gerbers have its nets' pads and its outline, its drill file its holes, its STEP and VRML files a
    header, and it draws (check_drawings), with those files as 3D models too (check_models) if models."""
    up = run(cli, ["fp", "upgrade", "-o", str(tmp / "upgraded.pretty"), str(FOOTPRINTS)], tmp / "upgraded.pretty")
    upgraded = sorted(p.stem for p in up.glob("*.kicad_mod") if p.read_text().startswith("(footprint "))
    if upgraded != EXPECTED_FOOTPRINTS:
        raise RuntimeError(f"fp upgrade wrote footprints {upgraded} in the current format, wanted {EXPECTED_FOOTPRINTS}")

    # A copy: with a fresh config, pcbnew writes the board's project settings (.kicad_prl) next to it.
    board = Path(shutil.copy2(BOARD, tmp))
    texts = export_gerbers(cli, board, tmp / "gerbers")
    nets = copper_nets(texts)
    if nets != EXPECTED:
        raise RuntimeError(f"unexpected gerber pads {nets}, wanted {EXPECTED}")
    if not any("%TF.FileFunction,Profile," in t for t in texts.values()):
        raise RuntimeError(f"no board outline (Edge_Cuts) among the gerbers {sorted(texts)}")

    drill = run(cli, ["pcb", "export", "drill", "-o", f"{tmp / 'drill'}/", str(board)], tmp / "drill")
    holes = sum(drill_holes(p.read_text()) for p in drill.glob("*.drl"))
    if holes != EXPECTED_HOLES:
        raise RuntimeError(f"the drill files have {holes} holes, wanted {EXPECTED_HOLES}")

    # STEP goes through opencascade, the bulk of pcbnew's libraries.
    origin = "{}x{}mm".format(*BOARD_CENTRE)
    step = run(cli, ["pcb", "export", "step", "--user-origin", origin, "-o", str(tmp / "board.step"), str(board)],
               tmp / "board.step")
    if not step.read_text().startswith("ISO-10303-21;"):
        raise RuntimeError("pcb export step wrote no STEP file")
    # VRML in tenths of an inch, the unit KiCad reads a VRML model in.
    vrml = run(cli, ["pcb", "export", "vrml", "--units", "tenths", "--user-origin", origin, "-o", str(tmp / "board.wrl"),
                     str(board)], tmp / "board.wrl")
    if not vrml.read_text().startswith("#VRML V2.0"):
        raise RuntimeError("pcb export vrml wrote no VRML file")

    bare = check_drawings(cli, board, BOARD_SIZE, tmp)
    if models:
        check_models(cli, bare, [step, vrml], tmp)


def with_model(board: str, path: str) -> str:
    """board's text with a 3D model (path, as a .kicad_pcb names it) on its first footprint."""
    start = board.index("(footprint ")
    depth = 0
    for tok in TOKEN.finditer(board, start):
        depth += {"(": 1, ")": -1}.get(tok.group(), 0)
        if depth == 0:
            return f"{board[:tok.start()]}\t{MODEL.format(path=path)}\n{board[tok.start():]}"
    raise RuntimeError("the board's first footprint never ends")


def render(cli: list[str], board: Path, out: Path) -> bytes:
    """board's `pcb render` from the top at RENDER_SIZE, written to out (checked to be that size)."""
    width, height = RENDER_SIZE
    png = run(cli, ["pcb", "render", "--side", "top", "-w", str(width), "-h", str(height), "-o", str(out),
                    str(board)], out).read_bytes()
    made = png_size(png)
    if any(not want - RENDER_SHORTFALL <= got <= want for got, want in zip(made, RENDER_SIZE)):
        raise RuntimeError(f"pcb render made a {made} image, wanted {RENDER_SIZE}")
    return png


def check_models(cli: list[str], bare: bytes, models: list[Path], tmp: Path) -> None:
    """Raise unless `pcb render` draws each of models (files KiCad's 3D plugins load: STEP, VRML) that BOARD
    brings next to it (${KIPRJMOD}), on a footprint: the render must differ from the bare one. Renders are
    deterministic, and without the plugins KiCad draws the board bare (#28)."""
    for model in models:
        folder = tmp / f"model-{model.suffix[1:]}"
        folder.mkdir()
        shutil.copy2(model, folder / f"model{model.suffix}")
        board = folder / BOARD.name
        board.write_text(with_model(BOARD.read_text(), f"${{KIPRJMOD}}/model{model.suffix}"))
        if render(cli, board, folder / "board.png") == bare:
            raise RuntimeError(f"pcb render drew no {model.suffix} 3D model: are the 3D plugins bundled?")


def check_drawings(cli: list[str], board: Path, size: tuple[float, float], tmp: Path) -> bytes:
    """Raise unless `pcb export svg` draws board (a board in a folder of its own, where pcbnew may write)
    the size of its outline (mm) and `pcb render` renders it in 3D; returns the render. Both run without a
    display (#20)."""
    svg = run(cli, ["pcb", "export", "svg", "--mode-single", "--layers", SVG_LAYERS, "--page-size-mode", "2",
                     "--exclude-drawing-sheet", "-o", str(tmp / "board.svg"), str(board)], tmp / "board.svg")
    drawn = svg_size(svg.read_text())
    if any(abs(a - b) > SVG_TOLERANCE for a, b in zip(drawn, size)):
        raise RuntimeError(f"pcb export svg drew {drawn} mm, wanted the board's {size}")
    return render(cli, board, tmp / "board.png")


def check(cli: list[str], models: bool = True) -> None:
    """Raise unless `cli sch export netlist` reproduces the expected nets, `cli sch erc` the expected
    errors (ERC also needs the cvpcb kiface) and pcbnew's commands their outputs (_check_pcbnew; models:
    renders draw 3D models, which a Linux bundle can only do when built from a simee/<version> branch)."""
    with tempfile.TemporaryDirectory() as tmp:
        net, erc = Path(tmp) / "smoke.net", Path(tmp) / "smoke-erc.json"
        nets = netlist_nets(run(cli, ["sch", "export", "netlist", "-o", str(net), str(SCHEMATIC)], net).read_text())
        errors = erc_errors(run(cli, ["sch", "erc", "--format", "json", "--severity-all", "-o", str(erc),
                                       str(SCHEMATIC)], erc).read_text())
        if nets != EXPECTED:
            raise RuntimeError(f"unexpected nets {nets}, wanted {EXPECTED}")
        if errors != EXPECTED_ERC:
            raise RuntimeError(f"unexpected ERC errors {errors}, wanted {EXPECTED_ERC}")
        _check_pcbnew(cli, Path(tmp), models)
