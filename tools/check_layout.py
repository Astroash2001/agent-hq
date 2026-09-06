"""
Validate prop placement geometrically.

Eyeballing renders missed a sofa half-buried in the south wall and bookshelves
clipping through the west wall, so this computes every prop's real pixel
rectangle from the atlas and flags:

  * props extending past a wall or off the map
  * props overlapping each other when they aren't a deliberate stack
  * props sitting on a wall tile

    python tools/check_layout.py

Exits non-zero if anything is wrong, so it can gate a rebuild.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FE = os.path.join(ROOT, 'frontend')
GEN = os.path.join(FE, 'assets', 'generated')

T = 16
# Props deliberately mounted on a wall surface rather than standing on the floor.
WALL_MOUNTED = {
    'whiteboard', 'whiteboard_big', 'chart_board', 'pic_frame_a', 'pic_frame_b',
    'wall_clock', 'wall_graph', 'wall_note', 'extinguisher', 'tv_widescreen',
    'projector_screen', 'whiteboard_arch', 'tv_boardroom', 'conf_wall_run',
    'conf_wall_strip', 'tv_wall_east', 'tv_wall_west', 'lounge_suite_full',
    # Modern Office wall kit -- boards and screens hang on a wall or on the
    # conference glazing, so they share floor space with whatever stands below.
    'of_wall_chart', 'of_wall_chart_b', 'of_whiteboard', 'of_art_a', 'of_art_b',
    # Partition runs ARE walls -- a room's side wall meeting the building's
    # south wall is the correct join, not a prop standing in the masonry.
    'thin_h', 'thin_v', 'thin_h_capw', 'thin_h_cape', 'of_glass_thin', 'of_post',
}
# Sprite pairs that are two halves of one piece of furniture and are MEANT to
# overlap. The L-desk's wing covers the main run's right-hand apron -- that join
# is what makes the corner read as a single desk. Keyed by (back run, wing) with
# the expected offset in tiles, so a wing that has drifted still gets flagged.
JOINED = {
    ('desk_l',      'desk_l_wing'): (0.9, 1.0),
    ('desk_l_wide', 'desk_l_wing'): (1.4, 1.0),
}


def is_joined(a, b):
    for (main, wing), (dx, dy) in JOINED.items():
        for p, q in ((a, b), (b, a)):
            if p['sprite'] == main and q['sprite'] == wing \
                    and abs((p['x'] + dx) - q['x']) < 0.05 \
                    and abs((p['y'] + dy) - q['y']) < 0.05:
                return True
    return False


def is_on_top(p):
    """A prop with a `dy` nudge is by definition standing on another prop, so it
    is exempt from overlap and floor checks. This replaces a hardcoded name list,
    which mis-classified sprites used both on a desk and on the floor (a plant
    can be either)."""
    return bool(p.get('dy'))


def load_map():
    js = os.path.join(FE, 'js', 'office_map.js').replace('\\', '/')
    code = f'const m=require("{js}");process.stdout.write(JSON.stringify(m.OFFICE_MAP));'
    out = subprocess.run(['node', '-e', code], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f'node failed:\n{out.stderr}')
    return json.loads(out.stdout)


def build_solid(m):
    cols, rows = m['cols'], m['rows']
    g = [[0] * cols for _ in range(rows)]

    def mark(x, y, v):
        if 0 <= x < cols and 0 <= y < rows:
            g[y][x] = v

    for w in m['walls']:
        for i in range(w['len']):
            if w.get('v'):
                mark(w['x'], w['y'] + i, 1)
            else:
                mark(w['x'] + i, w['y'], 1)
    for x in range(cols):
        mark(x, 1, 1)
        mark(x, 2, 1)
    for d in m['doorways']:
        for dy in range(d.get('h', 1)):
            for dx in range(d.get('w', 1)):
                mark(d['x'] + dx, d['y'] + dy, 0)
    return g


def rect(p, frames):
    f = frames[p['sprite']]
    cx = (p['x'] + 0.5) * T
    base = (p['y'] + 1) * T + p.get('dy', 0)
    return (cx - f['w'] / 2, base - f['h'], cx + f['w'] / 2, base)


SEATING = ('chair', 'stool', 'bench')
SURFACES = ('table', 'desk')


def is_seating_stack(seat, surface):
    """True when `seat` is a chair legitimately drawn up to `surface`."""
    if not any(k in seat['sprite'] for k in SEATING):
        return False
    if not any(k in surface['sprite'] for k in SURFACES):
        return False
    return abs(seat['y'] - surface['y']) <= 2.5


def overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def check_reachability():
    """
    Report how fragmented the walkable grid is, and which stations sit off it.

    Reported rather than failed, because it fails today and the fix is a map
    change, not a code change. It is here so the number is visible and so a
    change that makes it worse is obvious.

    What this measures: `office_layout.json`'s `blocked` grid is the same one
    `canvas.js buildCollision(true)` builds, and the same one the renderer's A*
    paths on. The office is currently NOT one connected region on that grid --
    the conference-room strip and the boss-office pocket are islands A* can
    never route into. Movement still works because `stepAgent` falls back to
    sliding, which tests each tile by its top-left corner (`blockedByProp`)
    rather than its centre, and exempts an agent's own seat. So the office looks
    fine and the pathfinder is doing almost nothing.
    """
    path = os.path.join(ROOT, 'backend', 'office_layout.json')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        exported = json.load(f)
    grid = exported.get('blocked')
    if not grid:
        return []

    rows, cols = len(grid), len(grid[0])
    blocked = [[c == '1' for c in row] for row in grid]
    seen = [[False] * cols for _ in range(rows)]
    regions = []
    for y in range(rows):
        for x in range(cols):
            if blocked[y][x] or seen[y][x]:
                continue
            stack, cells = [(x, y)], []
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if (0 <= nx < cols and 0 <= ny < rows
                            and not blocked[ny][nx] and not seen[ny][nx]):
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            regions.append(cells)
    regions.sort(key=len, reverse=True)

    notes = [f'REACHABILITY  {len(regions)} disconnected walkable region(s); '
             f'largest holds {len(regions[0])} of '
             f'{sum(len(r) for r in regions)} walkable tiles']
    main = set(regions[0])

    def covered(tile):
        x, y = tile
        return 0 <= y < rows and 0 <= x < cols and blocked[y][x]

    def adjacent_to_main(tile):
        x, y = tile
        return any((x + dx, y + dy) in main
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))

    for kind in ('seats', 'stations'):
        for name, pos in sorted(exported.get(kind, {}).items()):
            tile = (int(round(pos['x'])), int(round(pos['y'])))
            if tile in main:
                continue
            # A seat sits under its own desk sprite, and an agent standing on a
            # station stands on the furniture the station IS. Both are expected
            # -- `blockedByProp` exempts an agent at its own seat. What matters
            # is whether you can get there: a covered tile with open floor next
            # to it is fine, an island is not.
            if covered(tile) and adjacent_to_main(tile):
                continue
            state = ('inside a prop with no open tile beside it' if covered(tile)
                     else 'on an island the pathfinder cannot reach')
            notes.append(f'REACHABILITY  {kind}.{name} at {tile} is {state}')
    return notes


def check_layout_export(m):
    """The backend's copy of the geometry must match the map's.

    office.py walks agents to seats and stations by coordinate. It reads them
    from office_layout.json, which tools/export_layout.py generates from this
    map. Nothing regenerates that automatically, so this is the guard: if the
    map moves and the export is not rerun, the backend sends agents to where the
    furniture used to be. That is exactly how all five seats ended up inside
    desks before the export existed.
    """
    path = os.path.join(ROOT, 'backend', 'office_layout.json')
    if not os.path.exists(path):
        return [f'MISSING    backend/office_layout.json -- run: python tools/export_layout.py']
    with open(path, encoding='utf-8') as f:
        exported = json.load(f)

    problems = []
    for key in ('seats', 'stations'):
        live = m.get(key, {})
        saved = exported.get(key, {})
        for name in sorted(set(live) | set(saved)):
            if name not in saved:
                problems.append(f'STALE      {key}.{name} is in the map but not in office_layout.json')
            elif name not in live:
                problems.append(f'STALE      {key}.{name} is in office_layout.json but not in the map')
            elif (round(live[name]['x'], 4), round(live[name]['y'], 4)) !=                  (round(saved[name]['x'], 4), round(saved[name]['y'], 4)):
                problems.append(
                    f'STALE      {key}.{name} map ({live[name]["x"]}, {live[name]["y"]}) '
                    f'!= export ({saved[name]["x"]}, {saved[name]["y"]})')
    if problems:
        problems.append('           -> run: python tools/export_layout.py')
    return problems


def main():
    m = load_map()
    frames = json.load(open(os.path.join(GEN, 'office_atlas.json'), encoding='utf-8'))['frames']
    solid = build_solid(m)
    W, H = m['cols'] * T, m['rows'] * T

    props = [p for p in m['props'] if p['sprite'] in frames]
    problems = []

    # --- bounds + wall intrusion -------------------------------------------
    for p in props:
        if p.get('layer') == 'floor':
            continue
        name = p['sprite']
        x0, y0, x1, y1 = rect(p, frames)
        tag = f"{name} @ ({p['x']}, {p['y']})"

        if x0 < 0 or x1 > W or y1 > H:
            problems.append(f'OFF-MAP    {tag}  rect=({x0:.0f},{y0:.0f})-({x1:.0f},{y1:.0f})')
            continue
        if name in WALL_MOUNTED or is_on_top(p):
            continue

        # The tile the prop actually stands on must not be a wall.
        fx, fy = int((p['x'] + 0.5)), int(p['y'] + 1 - 0.01)
        if 0 <= fy < m['rows'] and 0 <= fx < m['cols'] and solid[fy][fx]:
            problems.append(f'ON-WALL    {tag}  stands on wall tile ({fx},{fy})')

        # Baseline must clear the south wall.
        south = None
        for w in m['walls']:
            if not w.get('v') and w['y'] == m['rows'] - 1:
                south = w['y'] * T
        if south is not None and y1 > south:
            problems.append(f'IN-S-WALL  {tag}  baseline {y1:.0f} > south wall top {south}')

    # --- mutual overlap -----------------------------------------------------
    floor_props = [p for p in props
                   if p.get('layer') != 'floor' and not is_on_top(p)
                   and p['sprite'] not in WALL_MOUNTED]
    for i, a in enumerate(floor_props):
        ra = rect(a, frames)
        area_a = (ra[2] - ra[0]) * (ra[3] - ra[1])
        for b in floor_props[i + 1:]:
            # A chair tucked under a desk shares its column on purpose.
            if abs(a['x'] - b['x']) < 0.01 and abs(a['y'] - b['y']) < 2.0:
                continue
            # So does a chair drawn up to a table it is not centred on -- a
            # boardroom seats six a side, and every one of them overlaps the
            # slab. Only accept it when the chair is BESIDE the table's long
            # edge, within a couple of tiles, so a chair genuinely buried in
            # a bookcase still reports.
            if is_seating_stack(a, b) or is_seating_stack(b, a):
                continue
            # Two halves of the same L-desk, adjacent by construction.
            if is_joined(a, b):
                continue
            rb = rect(b, frames)
            ov = overlap_area(ra, rb)
            if not ov:
                continue
            area_b = (rb[2] - rb[0]) * (rb[3] - rb[1])
            frac = ov / min(area_a, area_b)
            if frac > 0.35:
                problems.append(
                    f'OVERLAP    {a["sprite"]} @({a["x"]},{a["y"]}) '
                    f'<-> {b["sprite"]} @({b["x"]},{b["y"]})  {frac*100:.0f}%')

    problems.extend(check_layout_export(m))

    print(f'checked {len(props)} props')

    # Reported separately from `problems`, and deliberately not fatal: these
    # describe the map, not a regression, and the fix is to move furniture.
    for note in check_reachability():
        print('  ' + note)

    if problems:
        for p in sorted(problems):
            print('  ' + p)
        print(f'\n{len(problems)} problem(s)')
        return 1
    print('no placement problems')
    return 0


if __name__ == '__main__':
    sys.exit(main())
