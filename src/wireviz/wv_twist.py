"""
wireviz-twist  —  drop-in wrapper for wireviz that post-processes the SVG
output so that twisted-pair wires are drawn as sinusoidal curves with proper
over/under crossings instead of flat horizontal colour bars.

Usage:  wireviz-twist [wireviz options] <file.yml> [<file.yml> ...]

The YAML cables section supports the extra key:
    twists:
      - [2, 3]      # wire numbers (1-based) that form a twisted pair

All other wireviz options are forwarded unchanged.
"""

import math
import re
import subprocess
import sys
from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

_FALLBACK = {
    'BK': '#000000', 'WH': '#ffffff', 'GY': '#808080', 'PK': '#ff69b4',
    'RD': '#ff0000', 'GN': '#00aa00', 'BU': '#0000ff', 'TQ': '#00ced1',
    'YE': '#ffff00', 'OG': '#ff8000', 'VT': '#9400d3', 'BN': '#8b4513',
}


def _to_hex(code: str) -> str:
    """WireViz colour code → '#RRGGBB'."""
    try:
        from wireviz.wv_colors import get_color_hex
        result = get_color_hex(code)
        if result:
            h = result[0]
            return h if h.startswith('#') else f'#{h}'
    except Exception:
        pass
    return _FALLBACK.get(code.upper()[:2], '#888888')


def _extract_path_endpoints(d: str):
    """Extract (x_start, y_start, x_end, y_end) from an SVG path 'd' attribute."""
    start_m = re.match(r'M\s*([\-\d.]+),([\-\d.]+)', d.strip())
    end_m   = re.search(r'([\-\d.]+),([\-\d.]+)\s*$', d.strip())
    if start_m and end_m:
        return (float(start_m.group(1)), float(start_m.group(2)),
                float(end_m.group(1)),   float(end_m.group(2)))
    return None


def _edge_y_center(edge_html: str) -> float:
    """Average y across all path start/end points in an edge group."""
    ys = []
    for d in re.findall(r'<path[^>]+ d="([^"]+)"', edge_html):
        ep = _extract_path_endpoints(d)
        if ep:
            ys.extend([ep[1], ep[3]])
    return sum(ys) / len(ys) if ys else 0.0


def _get_wire_center(edge_html: str):
    """
    Return (x_start, y_start, x_end, y_end) for the center path of a wire edge.
    Prefers the non-black (coloured) path; falls back to the middle path by y.
    """
    items = []
    for color, d in re.findall(
            r'<path fill="none" stroke="([^"]+)"[^>]+ d="([^"]+)"', edge_html):
        ep = _extract_path_endpoints(d)
        if ep:
            items.append((color, ep))
    if not items:
        return None
    for color, ep in items:
        if color.lower() not in ('#000000', 'black'):
            return ep
    # All black: use the middle path by y-sort
    items.sort(key=lambda x: x[1][1])
    return items[len(items) // 2][1]


# --------------------------------------------------------------------------- #
#  Sinusoidal braid for wire edges                                             #
# --------------------------------------------------------------------------- #

def _twist_paths_for_edges(x1, x2, yA1, yA2, yB1, yB2,
                            color_a, color_b,
                            periods=2, sw=3.0):
    """
    Sinusoidal braid for two twisted wire edges. Uses cosine to guarantee
    exact endpoint y-positions (cos(0) = cos(2·pi·k) = 1 for integer k).

    Wire A: starts at (x1, yA1) and ends at (x2, yA2).
    Wire B: starts at (x1, yB1) and ends at (x2, yB2).
    """
    W  = x2 - x1
    cy1 = (yA1 + yB1) / 2
    cy2 = (yA2 + yB2) / 2
    # per-wire offsets relative to the centre line at start and end
    oA1, oA2 = yA1 - cy1, yA2 - cy2
    oB1, oB2 = yB1 - cy1, yB2 - cy2

    def wy(which, x):
        t  = (x - x1) / W
        cy = cy1 + t * (cy2 - cy1)
        o  = (oA1 + t*(oA2-oA1)) if which == 'A' else (oB1 + t*(oB2-oB1))
        return cy + o * math.cos(2 * math.pi * periods * t)

    def seg(which, xs, xe, n=80):
        pts = [f"{xs+(xe-xs)*j/n:.2f},{wy(which,xs+(xe-xs)*j/n):.2f}"
               for j in range(n+1)]
        return 'M ' + ' L '.join(pts)

    def path_el(d, color, width=None):
        w = width if width is not None else sw
        return (f'<path d="{d}" stroke="{color}" stroke-width="{w:.1f}"'
                f' fill="none"/>')

    # Crossings where cos(2π·periods·t) = 0  →  t = (2m+1) / (4·periods)
    n_cx = 4 * periods
    cx_xs = sorted({max(x1, min(x2, x1 + (2*m+1)/n_cx * W))
                    for m in range(n_cx)})
    bps = sorted({x1} | set(cx_xs) | {x2})

    segs = []
    for i in range(len(bps)-1):
        xs, xe = bps[i], bps[i+1]
        if xs >= xe:
            continue
        t_m   = ((xs+xe)/2 - x1) / W
        cos_v = math.cos(2 * math.pi * periods * t_m)
        # oA < 0 (A starts above), oB > 0 → A above B when cos_v > 0
        tw, bw = ('A', 'B') if cos_v > 0 else ('B', 'A')
        segs.append((xs, xe, tw, bw))

    cm = {'A': color_a, 'B': color_b}

    # Three-pass painter: under wires → white knockout → over wires.
    # The knockout (slightly thicker white stroke along the over-wire path)
    # masks the under wire at the crossing without leaving an empty gap.
    result  = [path_el(seg(bw, xs, xe), cm[bw])           for xs, xe, tw, bw in segs]
    result += [path_el(seg(tw, xs, xe), '#ffffff', sw*2.5) for xs, xe, tw, bw in segs]
    result += [path_el(seg(tw, xs, xe), cm[tw])            for xs, xe, tw, bw in segs]
    return result


# --------------------------------------------------------------------------- #
#  SVG post-processor — edge braid                                             #
# --------------------------------------------------------------------------- #

def _post_process_edges(svg_path: Path, cable_infos: dict) -> None:
    """
    For every cable with twists, replace the straight edge lines for the
    twisted wires with sinusoidal braid paths (over/under crossings).
    The cable-box swatch table is left completely unchanged.
    """
    from html import unescape
    text = svg_path.read_text('utf-8')

    for cable_name, info in cable_infos.items():
        twists     = info['twists']
        hex_colors = info['hex_colors']

        # ---- find edge groups connected to this cable -------------------- #
        edge_re = re.compile(
            r'(<g id="edge\d+" class="edge">)(.*?)(</g>)', re.DOTALL)
        all_edges = list(edge_re.finditer(text))

        left_edges, right_edges = [], []
        for m in all_edges:
            title_m = re.search(r'<title>([^<]+)</title>', m.group(2))
            if not title_m:
                continue
            title = unescape(title_m.group(1))
            if f':e--{cable_name}:w' in title:
                left_edges.append(m)
            elif f'{cable_name}:e--' in title:
                right_edges.append(m)

        if not left_edges or not right_edges:
            print(f'  Warning: no edges found for cable "{cable_name}"',
                  file=sys.stderr)
            continue

        # Sort by y-centre so index 0 = wire 1 (topmost), etc.
        left_edges.sort(key=lambda m: _edge_y_center(m.group()))
        right_edges.sort(key=lambda m: _edge_y_center(m.group()))

        replacements = {}   # start_pos → (end_pos, new_html)

        for group in twists:
            group_0 = [w - 1 for w in group]   # 1-based → 0-based
            if len(group_0) != 2:
                continue   # only pairs for now

            for side in (left_edges, right_edges):
                if not all(0 <= wi < len(side) for wi in group_0):
                    print(f'  Warning: {cable_name}: wire group {group} out of range',
                          file=sys.stderr)
                    continue

                em_a = side[group_0[0]]   # edge match for wire A (upper)
                em_b = side[group_0[1]]   # edge match for wire B (lower)

                cp_a = _get_wire_center(em_a.group())
                cp_b = _get_wire_center(em_b.group())
                if not cp_a or not cp_b:
                    continue

                x1 = min(cp_a[0], cp_b[0])
                x2 = max(cp_a[2], cp_b[2])

                new_paths = _twist_paths_for_edges(
                    x1, x2,
                    cp_a[1], cp_a[3],              # yA1, yA2
                    cp_b[1], cp_b[3],              # yB1, yB2
                    hex_colors[group_0[0]],
                    hex_colors[group_0[1]])

                def _strip_paths(html):
                    return re.sub(r'\s*<path[^/]*/>', '', html)

                # First edge group: remove old paths, insert braid
                new_a = (em_a.group(1)
                         + _strip_paths(em_a.group(2))
                         + '\n' + '\n'.join(new_paths) + '\n'
                         + em_a.group(3))
                # Second edge group: remove old paths (braid lives in first)
                new_b = (em_b.group(1)
                         + _strip_paths(em_b.group(2))
                         + em_b.group(3))

                replacements[em_a.start()] = (em_a.end(), new_a)
                replacements[em_b.start()] = (em_b.end(), new_b)

        for start in sorted(replacements, reverse=True):
            end, new_text = replacements[start]
            text = text[:start] + new_text + text[end:]

    svg_path.write_text(text, 'utf-8')


# --------------------------------------------------------------------------- #
#  PNG regeneration from modified SVG                                          #
# --------------------------------------------------------------------------- #

def _svg_to_png(svg_path: Path) -> None:
    png_path = svg_path.with_suffix('.png')
    for cmd in (
        ['rsvg-convert', '-o', str(png_path), str(svg_path)],
        ['inkscape', '--export-filename', str(png_path), str(svg_path)],
        ['cairosvg', str(svg_path), '-o', str(png_path)],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0:
                return
        except FileNotFoundError:
            continue
    print('  Note: could not regenerate PNG (rsvg-convert/inkscape/cairosvg not found).',
          file=sys.stderr)


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    # Collect YAML files from the argument list (last positional args ending in .yml/.yaml)
    yml_files = [Path(a) for a in args if a.endswith(('.yml', '.yaml'))]

    # Run wireviz with all arguments as-is
    import shutil
    wireviz_bin = Path(sys.argv[0]).parent / 'wireviz'
    if not wireviz_bin.exists():
        found = shutil.which('wireviz')
        wireviz_bin = Path(found) if found else wireviz_bin

    result = subprocess.run([str(wireviz_bin)] + args, capture_output=False)
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Post-process each YAML's SVG output
    for yml_path in yml_files:
        if not yml_path.exists():
            continue

        with open(yml_path) as f:
            data = yaml.safe_load(f)

        cables = data.get('cables', {})
        cable_infos = {}

        for cable_name, cdata in cables.items():
            twists = cdata.get('twists')
            if not twists:
                continue

            colors = list(cdata.get('colors', []))
            wirecount = cdata.get('wirecount', len(colors))
            if wirecount > len(colors) > 0:
                m = wirecount // len(colors) + 1
                colors = (colors * m)[:wirecount]

            cable_infos[cable_name] = {
                'twists':     twists,
                'hex_colors': [_to_hex(c) for c in colors],
            }

        if not cable_infos:
            continue

        svg_path = yml_path.with_suffix('.svg')
        if not svg_path.exists():
            print(f'  SVG not found: {svg_path}', file=sys.stderr)
            continue

        print(f'Applying twist visualisation → {svg_path.name}')
        _post_process_edges(svg_path, cable_infos)
        _svg_to_png(svg_path)


if __name__ == '__main__':
    main()
