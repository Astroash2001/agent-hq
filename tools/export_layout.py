"""
Export the office geometry the backend needs, straight out of the map.

`office.py` has to know where the desks and stations are, so it can walk agents
to them. It used to keep its own copy of those coordinates, and that copy went
stale the moment the map was rearranged -- by the time this script was written
every one of the five backend seats had drifted into the middle of a desk
sprite, and two stations sat inside the boardroom table.

So the map owns the geometry and the backend reads it. This writes:

    backend/office_layout.json

Run it whenever seats or stations move in office_map.js. check_layout.py fails
if the file is missing or out of date, so the drift cannot happen quietly again.

    python tools/export_layout.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(ROOT, 'frontend', 'js', 'office_map.js')
ATLAS = os.path.join(ROOT, 'frontend', 'assets', 'generated', 'office_atlas.json')
OUT = os.path.join(ROOT, 'backend', 'office_layout.json')

T = 16


def read_map():
    """The map through node, so there is never a second parser to keep in step."""
    code = f'const m=require("{MAP.replace(chr(92), "/")}");process.stdout.write(JSON.stringify(m.OFFICE_MAP));'
    r = subprocess.run(['node', '-e', code], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'node failed reading office_map.js:\n{r.stderr}')
    return json.loads(r.stdout)


# Mirrors `canvas.js buildPropRects()`. Kept as data next to the code that uses
# it so the two can be diffed by eye; if the renderer's list changes, this one
# has to change with it or the backend and the office disagree about the floor.
SEATING = ('chair',)
WALL_ART = ('wall_chart', 'whiteboard', 'wall_clock', 'wall_note', 'wall_graph',
            'pic_frame', 'of_art', 'wall_art', 'extinguisher', 'chart_board')


def build_blocked(m, frames):
    """
    The grid an agent may not walk through, exactly as `canvas.js
    buildCollision(true)` builds it.

    Exported because the backend times walks as straight-line distance while the
    renderer paths around furniture, so the two disagree on any route that has
    to go round something. Getting the backend onto the same grid is the fix;
    this is the grid.

    "Exactly as" is the whole job, and the exemptions are most of it. A first
    version of this function marked every prop and reported the office as seven
    disconnected islands -- which was wrong, and wrong in the direction that
    invents a bug. The renderer leaves out floor-layer props (rugs are floor,
    you walk on them), anything with a `dy` (it stands on another prop), small
    chairs (agents are meant to end up on them, and the conference seats ring
    the table so tightly that blocking them walls the room off), and wall art
    (it hangs on the wall course and is not in the room). Then it punches the
    seats back out, because a desk covers the tile its own chair stands on.
    """
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

    rects = []
    for prop in m['props']:
        sprite = prop['sprite']
        if sprite.startswith('thin_'):
            # The conference room's partitions are props rather than `walls`
            # entries, and are the only ones marked as a tile rather than a rect.
            mark(int(prop['x']), int(prop['y']), 1)
            continue
        if prop.get('layer') == 'floor' or prop.get('dy') is not None:
            continue
        if any(k in sprite for k in SEATING):
            continue
        if any(k in sprite for k in WALL_ART):
            continue
        frame = frames.get(sprite)
        if not frame:
            continue
        left = round((prop['x'] + 0.5) * T - frame['w'] / 2)
        bottom = round((prop['y'] + 1) * T)
        rects.append((left, bottom - frame['h'], left + frame['w'], bottom))

    for ty in range(rows):
        for tx in range(cols):
            px, py = tx * T + T / 2, ty * T + T / 2
            for left, top, right, bottom in rects:
                if left <= px < right and top <= py < bottom:
                    mark(tx, ty, 1)
                    break

    # A desk covers the tile its own chair stands on, so seats come back out or
    # nobody can be routed to their own desk.
    for seat in m.get('seats', {}).values():
        mark(int(seat['x']), int(seat['y']), 0)

    # The spare desks too, for the same reason: a contractor hired mid-mission
    # has to be able to stand on the seat it is sent to.
    for seat in m.get('spawnSeats', []):
        mark(int(seat['x']), int(seat['y']), 0)

    # Doorways last, so nothing above can seal one.
    for d in m['doorways']:
        for dy in range(d.get('h', 1)):
            for dx in range(d.get('w', 1)):
                mark(d['x'] + dx, d['y'] + dy, 0)

    return g


def build(m, frames):
    blocked = build_blocked(m, frames)
    return {
        '_generated_by': 'tools/export_layout.py -- do not edit by hand',
        '_source': 'frontend/js/office_map.js',
        'tile': m['tile'],
        'cols': m['cols'],
        'rows': m['rows'],
        'seats': {k: {'x': v['x'], 'y': v['y'], 'facing': v.get('facing', 'down')}
                  for k, v in m['seats'].items()},
        'stations': {k: {'x': v['x'], 'y': v['y'], 'label': v.get('label', k)}
                     for k, v in m.get('stations', {}).items()},
        # Desks for agents hired mid-mission, and the door they arrive through.
        'spawn_seats': [{'x': s['x'], 'y': s['y'], 'facing': s.get('facing', 'down')}
                        for s in m.get('spawnSeats', [])],
        'entrance': dict(m.get('entrance') or {'x': 24.5, 'y': 22.4}),
        # Rows of "0110" rather than nested lists: 24 short strings instead of
        # 960 integers, and it stays readable in a diff when the map moves.
        'blocked': [''.join(str(c) for c in row) for row in blocked],
    }


def read_frames():
    with open(ATLAS, encoding='utf-8') as f:
        atlas = json.load(f)
    return atlas.get('frames', atlas)


def main():
    payload = build(read_map(), read_frames())
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.write('\n')
    print(f'wrote {OUT}')
    walkable = sum(row.count('0') for row in payload['blocked'])
    print(f'  {len(payload["seats"])} seats, {len(payload["spawn_seats"])} spawn seats, '
          f'{len(payload["stations"])} stations, {walkable} walkable tiles')


if __name__ == '__main__':
    main()
