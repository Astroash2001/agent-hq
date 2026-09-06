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


def build_blocked(m, frames):
    """
    The grid an agent may not walk through, as `canvas.js buildCollision(true)`
    builds it: masonry, minus doorways, plus the conference partitions, plus any
    tile whose CENTRE is covered by a prop.

    Exported because the backend was timing walks as straight-line distance
    while the renderer pathed around the furniture. The two disagreed on every
    route that has to go round the conference room -- the sprite was still
    walking when the backend had already decided it had arrived.

    A tile counts as furniture only when its centre is covered. Marking every
    tile a sprite so much as touches was measured on this map and left exactly
    one square walkable.
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
    for d in m['doorways']:
        for dy in range(d.get('h', 1)):
            for dx in range(d.get('w', 1)):
                mark(d['x'] + dx, d['y'] + dy, 0)

    rects = []
    for prop in m['props']:
        sprite = prop['sprite']
        if sprite.startswith('thin_'):
            mark(int(prop['x']), int(prop['y']), 1)
        frame = frames.get(sprite)
        if not frame:
            continue
        cx = (prop['x'] + 0.5) * T
        base = (prop['y'] + 1) * T + prop.get('dy', 0)
        rects.append((cx - frame['w'] / 2, base - frame['h'],
                      cx + frame['w'] / 2, base))

    for ty in range(rows):
        for tx in range(cols):
            px, py = tx * T + T / 2, ty * T + T / 2
            for left, top, right, bottom in rects:
                if left <= px < right and top <= py < bottom:
                    mark(tx, ty, 1)
                    break

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
    print(f'  {len(payload["seats"])} seats, {len(payload["stations"])} stations, '
          f'{walkable} walkable tiles')


if __name__ == '__main__':
    main()
