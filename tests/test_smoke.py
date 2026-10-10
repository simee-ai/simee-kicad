import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from kicad_bundle import linux, macos, smoke, windows_build
from kicad_bundle.smoke import (BOARD_SIZE, EXPECTED, EXPECTED_ERC, EXPECTED_FOOTPRINTS, EXPECTED_HOLES, RENDER_SIZE,
                                drill_holes, erc_errors, gerber_nets, netlist_nets, png_size, svg_size)

NETLIST = """(export (version "E")
  (nets
    (net (code "1") (name "/IN") (class "Default") (node (ref "R1") (pin "1") (pintype "passive")))
    (net (code "2") (name "/OUT") (class "Default")
      (node (ref "C1") (pin "2") (pinfunction "~_2") (pintype "passive"))
      (node (ref "R1") (pin "2") (pintype "passive")))
    (net (code "3") (name "GND") (class "Default") (node (ref "C1") (pin "1") (pintype "passive")))))
"""


def _violation(type_: str, severity: str, item: str) -> dict:
    return {"type": type_, "severity": severity, "description": "", "items": [{"description": item}]}


# What kicad-cli 10.0.6 reports for the smoke schematic (`sch erc --format json --severity-all`).
ERC_REPORT = json.dumps({"$schema": "https://schemas.kicad.org/erc.v1.json", "sheets": [{"path": "/", "violations": [
    _violation("power_pin_not_driven", "error", "Symbol #PWR01 Pin 1 [Power input, Line]"),
    _violation("endpoint_off_grid", "warning", "Symbol R1 Pin 1 [Passive, Line]"),
    _violation("lib_symbol_issues", "warning", "Symbol C1 [C]"),
]}]})


def test_netlist_nets_reads_kicadsexpr():
    assert netlist_nets(NETLIST) == EXPECTED


def test_erc_errors_reads_the_error_violations_of_a_json_report():
    assert erc_errors(ERC_REPORT) == EXPECTED_ERC


# Trimmed from kicad-cli 10.0.6's `pcb export gerbers` of the smoke board (F_Cu; B_Cu has the same pads).
GERBER = """%TF.FileFunction,Copper,L1,Top*%
%FSLAX46Y46*%
D10*
%TO.P,R1,1*%
%TO.N,/IN*%
X105000000Y-100000000D03*
D11*
%TO.P,R1,2*%
%TO.N,/OUT*%
X110080000Y-100000000D03*
%TD*%
D10*
%TO.P,C1,1*%
%TO.N,GND*%
X115160000Y-100000000D03*
%TO.P,C1,2,~_2*%
%TO.N,/OUT*%
X112620000Y-100000000D03*
%TD*%
%TO.N,/OUT*%
D12*
X110080000Y-100000000D02*
X112620000Y-100000000D01*
%TD*%
M02*
"""
EDGE_CUTS = "%TF.FileFunction,Profile,NP*%\nM02*\n"
DRILL = """M48
METRIC
T1C0.800
%
G90
G05
T1
X105.0Y-100.0
X110.08Y-100.0
X112.62Y-100.0
X115.16Y-100.0
M30
"""
FOOTPRINT = '(footprint "R_Axial_P5.08mm"\n\t(version 20260206)\n)\n'
STEP = "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
VRML = "#VRML V2.0 utf8\nWorldInfo { title \"rc_filter\" }\n"
# Trimmed from kicad-cli 10.0.6's `pcb export svg` of the smoke board (board area only).
SVG = """<?xml version="1.0" standalone="no"?>
 <!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"
 "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns:svg="http://www.w3.org/2000/svg" xmlns="http://www.w3.org/2000/svg" version="1.1"
  width="19.9898mm" height="9.9822mm" viewBox="0.0000 0.0000 19.9898 9.9822">
<g style="fill:#C83434; fill-opacity:1.0000; stroke:none;">
<circle cx="5.0000" cy="5.0000" r="0.8000" />
</g>
</svg>
"""


def _png(width: int, height: int) -> bytes:
    """A PNG's signature and header chunk: all png_size reads."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height) + b"\x08\x06\x00\x00\x00"

# What each command writes: (file under its -o folder, or "" when -o is the file; text).
OUTPUTS = {
    "sch export netlist": [("", NETLIST)],
    "sch erc": [("", ERC_REPORT)],
    "fp upgrade": [("R_Axial_P5.08mm.kicad_mod", FOOTPRINT)],
    "pcb export gerbers": [("rc_filter-F_Cu.gtl", GERBER), ("rc_filter-B_Cu.gbl", GERBER.replace("L1,Top", "L2,Bot")),
                           ("rc_filter-Edge_Cuts.gm1", EDGE_CUTS)],
    "pcb export drill": [("rc_filter.drl", DRILL)],
    "pcb export step": [("", STEP)],
    "pcb export vrml": [("", VRML)],
    "pcb export svg": [("", SVG)],
    "pcb render": [("", _png(*RENDER_SIZE))],
}

FAKE_CLI = """import re
import sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("-o") + 1])
outputs = {outputs!r}
cmd = next(c for c in outputs if args[:len(c.split())] == c.split())
if cmd == {fail!r}:
    {code}
board = Path(args[-1]).read_text() if args[-1].endswith(".kicad_pcb") else ""
for name, text in outputs[cmd]:
    if cmd == "pcb render" and {draws_models!r}:  # a model changes the image
        text += "".join(re.findall(r'[(]model "[^"]+"', board)).encode()
    if name:
        out.mkdir(parents=True, exist_ok=True)
    path = out / name if name else out
    path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)
"""


def _fake_cli(tmp_path: Path, fail: str = "", code: str = "pass", draws_models: bool = True,
              **outputs: list) -> list[str]:
    """A kicad-cli that writes OUTPUTS (or outputs, keyed by command with _ for spaces), except that
    the command fail runs code first. Its renders draw a board's 3D models unless draws_models is False."""
    script = tmp_path / "kicad_cli.py"
    script.write_text(FAKE_CLI.format(outputs={**OUTPUTS, **{k.replace("_", " "): v for k, v in outputs.items()}},
                                      fail=fail, code=code, draws_models=draws_models))
    return [sys.executable, str(script)]


def test_gerber_nets_reads_the_pads_of_each_net():
    assert gerber_nets([GERBER, GERBER]) == EXPECTED


def test_gerber_nets_leaves_out_pads_on_no_net():
    # an imported board's fiducials and mounting holes
    nc = "%TO.P,U$5,1*%\n%TO.N,N/C*%\nX0Y0D03*\n%TO.P,U$7,P$1*%\n%TO.N,N/C*%\nX1Y1D03*\n"
    assert gerber_nets([GERBER, nc]) == EXPECTED


def test_drill_holes_counts_the_holes_of_an_excellon_file():
    assert drill_holes(DRILL) == EXPECTED_HOLES


def test_check_runs_erc_and_the_pcb_commands(tmp_path):
    smoke.check(_fake_cli(tmp_path))


def test_check_fails_when_erc_cant_load_a_kiface(tmp_path):
    # The trimmed bundle's failure before cvpcb was bundled: ERC exits 1 and writes no report.
    erc = ("print(\"Error: Failed to load kiface library '/x/PlugIns/_cvpcb.kiface'.\", file=sys.stderr); "
           "sys.exit(1)")
    with pytest.raises(RuntimeError, match="_cvpcb.kiface"):
        smoke.check(_fake_cli(tmp_path, "sch erc", erc))


@pytest.mark.parametrize("command", ["fp upgrade", "pcb export gerbers", "pcb export drill", "pcb export step",
                                     "pcb export vrml", "pcb export svg", "pcb render"])
def test_check_fails_when_a_pcb_command_cant_load_pcbnew(tmp_path, command):
    # The trimmed bundle's failure before pcbnew was bundled (simee-kicad#8): exit 255, nothing written.
    fail = ("print(\"Error: Failed to load kiface library '/x/PlugIns/_pcbnew.kiface'.\", file=sys.stderr); "
            "sys.exit(255)")
    with pytest.raises(RuntimeError, match="_pcbnew.kiface"):
        smoke.check(_fake_cli(tmp_path, command, fail))


def test_check_fails_when_the_gerbers_lose_a_pad(tmp_path):
    gerber = GERBER.replace("%TO.N,GND*%", "%TO.N,/OUT*%")
    with pytest.raises(RuntimeError, match="gerber"):
        smoke.check(_fake_cli(tmp_path, pcb_export_gerbers=[("rc_filter-F_Cu.gtl", gerber),
                                                            ("rc_filter-Edge_Cuts.gm1", EDGE_CUTS)]))


def test_check_fails_without_a_board_outline(tmp_path):
    with pytest.raises(RuntimeError, match="Edge_Cuts"):
        smoke.check(_fake_cli(tmp_path, pcb_export_gerbers=[("rc_filter-F_Cu.gtl", GERBER)]))


def test_check_fails_when_fp_upgrade_leaves_the_old_format(tmp_path):
    old = "(module R_Axial_P5.08mm (layer F.Cu))\n"
    with pytest.raises(RuntimeError, match="fp upgrade"):
        smoke.check(_fake_cli(tmp_path, fp_upgrade=[("R_Axial_P5.08mm.kicad_mod", old)]))


def test_check_fails_on_a_step_file_that_isnt_one(tmp_path):
    with pytest.raises(RuntimeError, match="STEP"):
        smoke.check(_fake_cli(tmp_path, pcb_export_step=[("", "")]))


def test_check_fails_when_the_render_draws_no_3d_models(tmp_path):
    # A bundle without the 3D plugins renders a board that brings its own models bare (simee-kicad#28).
    with pytest.raises(RuntimeError, match="model"):
        smoke.check(_fake_cli(tmp_path, draws_models=False))


def test_check_can_leave_the_3d_models_out(tmp_path):
    # An official Linux bundle has the plugins but no way to point KiCad at them (simee/<version> adds it).
    smoke.check(_fake_cli(tmp_path, draws_models=False), models=False)


def test_with_model_gives_the_first_footprint_a_3d_model():
    board = smoke.parse_sexpr(smoke.with_model(smoke.BOARD.read_text(), "${KIPRJMOD}/model.step"))
    footprints = [c for c in board if isinstance(c, list) and c[0] == "footprint"]
    models = [[m[1] for m in fp if isinstance(m, list) and m[0] == "model"] for fp in footprints]
    assert models[0] == ["${KIPRJMOD}/model.step"]
    assert not any(models[1:])


def test_svg_size_reads_the_drawings_size_in_mm():
    assert svg_size(SVG) == (19.9898, 9.9822)


def test_png_size_reads_the_header():
    assert png_size(_png(640, 360)) == (640, 360)


def test_png_size_rejects_what_isnt_a_png():
    with pytest.raises(RuntimeError, match="PNG"):
        png_size(b"JFIF" + bytes(30))


def test_check_fails_when_the_svg_isnt_the_boards_size(tmp_path):
    # e.g. a page with a drawing sheet instead of the board area
    a4 = SVG.replace('width="19.9898mm" height="9.9822mm"', 'width="297.0022mm" height="210.0072mm"')
    with pytest.raises(RuntimeError, match="svg"):
        smoke.check(_fake_cli(tmp_path, pcb_export_svg=[("", a4)]))


def test_check_takes_kicads_slightly_smaller_render(tmp_path):
    # what kicad-cli 10.0.6 makes of 400 x 200
    smoke.check(_fake_cli(tmp_path, pcb_render=[("", _png(368, 168))]))


def test_check_fails_when_the_render_isnt_the_size_asked_for(tmp_path):
    with pytest.raises(RuntimeError, match="render"):
        smoke.check(_fake_cli(tmp_path, pcb_render=[("", _png(1600, 900))]))


def test_the_smoke_board_is_the_rc_filters_layout():
    # Its pads carry the schematic's nets, so gerber_nets of its copper layers is EXPECTED.
    board = smoke.parse_sexpr(smoke.BOARD.read_text())
    pads = defaultdict(list)
    for fp in (c for c in board if isinstance(c, list) and c[0] == "footprint"):
        ref = next(c[2] for c in fp if isinstance(c, list) and c[:2] == ["property", "Reference"])
        for pad in (c for c in fp if isinstance(c, list) and c[0] == "pad"):
            pads[next(c[1] for c in pad if isinstance(c, list) and c[0] == "net")].append(f"{ref}.{pad[1]}")
    assert sorted(sorted(p) for p in pads.values()) == EXPECTED
    assert len([p for pins in pads.values() for p in pins]) == EXPECTED_HOLES  # every pad is through-hole
    assert sorted(p.stem for p in smoke.FOOTPRINTS.glob("*.kicad_mod")) == EXPECTED_FOOTPRINTS
    rect = next(c for c in board if isinstance(c, list) and c[0] == "gr_rect")
    (x0, y0), (x1, y1) = (map(float, next(c[1:] for c in rect if isinstance(c, list) and c[0] == k)) for k in ("start", "end"))
    assert (x1 - x0, y1 - y0) == BOARD_SIZE
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == smoke.BOARD_CENTRE


@pytest.mark.parametrize("kiface", ["cvpcb", "pcbnew"])
def test_every_platform_bundles_and_builds_the_kifaces_simee_runs(kiface):
    # `sch erc` loads cvpcb's kiface for its footprint checks (simee-kicad#7); every `fp` and `pcb`
    # command loads pcbnew's (simee-kicad#8). Without them those commands fail.
    assert f"PlugIns/_{kiface}.kiface" in macos.ROOTS
    assert f"usr/bin/_{kiface}.kiface" in linux.roots(linux.OFFICIAL)
    assert f"{kiface}_kiface" in windows_build.TARGETS


@pytest.mark.parametrize("plugin", ["idf", "oce", "vrml"])
def test_every_platform_bundles_and_builds_the_3d_model_plugins(plugin):
    # KiCad dlopens them from its plugin folder, so no binary's imports reach them (simee-kicad#28).
    assert f"PlugIns/3d/libs3d_plugin_{plugin}.so" in macos.ROOTS
    assert f"usr/lib/x86_64-linux-gnu/kicad/plugins/3d/libs3d_plugin_{plugin}.so" in linux.roots(linux.OFFICIAL)
    assert f"usr/lib/aarch64-linux-gnu/kicad/plugins/3d/libs3d_plugin_{plugin}.so" in linux.roots(linux.ARCHES["arm64"])
    assert f"s3d_plugin_{plugin}" in windows_build.TARGETS
    assert f"plugins/3d/s3d_plugin_{plugin}.dll" in windows_build.PLUGINS  # under bin/
