"""
Render the office to a PNG without a browser.

Reads the real office_map.js through node (so there is no second copy of the
layout to drift), then composites the same layers canvas.js draws: floor,
walls, depth-sorted props and seated agents.

    python tools/preview_map.py

Writes tools/_slices/office_preview.png
"""
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FE = os.path.join(ROOT, 'frontend')
GEN = os.path.join(FE, 'assets', 'generated')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_slices')

FLOOR_VARIANTS = 4
FLOOR_BLOCK_H = 48

# Kept in step with the same constants in frontend/js/canvas.js.
SHELL_RADIUS = 6
SHELL_RIM = 3
SHELL_EDGE_W = 1
SHELL_RIM_COLOR = (205, 113, 66)
SHELL_EDGE = (74, 34, 31)


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


def main():
    m = load_map()
    T = m['tile']
    W, H = m['cols'] * T, m['rows'] * T

    atlas = Image.open(os.path.join(GEN, 'office_atlas.png')).convert('RGBA')
    man = json.load(open(os.path.join(GEN, 'office_atlas.json'), encoding='utf-8'))
    frames = man['frames']
    chars = man['characters']
    floor = Image.open(os.path.join(GEN, 'floor_wood.png')).convert('RGBA')

    canvas = Image.new('RGBA', (W, H), (0, 0, 0, 0))

    def sprite(name):
        f = frames[name]
        return atlas.crop((f['x'], f['y'], f['x'] + f['w'], f['y'] + f['h']))

    # --- floor, with the same deterministic variant scatter as canvas.js ----
    for ty in range(m['rows']):
        for tx in range(m['cols']):
            v = ((tx * 73856093) ^ (ty * 19349663)) % FLOOR_VARIANTS
            sy = (ty * T) % FLOOR_BLOCK_H
            canvas.alpha_composite(floor.crop((v * T, sy, v * T + T, sy + T)), (tx * T, ty * T))

    # --- rugs: floor overlay, before walls and props ------------------------
    for p in m['props']:
        if p.get('layer') != 'floor' or p['sprite'] not in frames:
            continue
        im = sprite(p['sprite'])
        canvas.alpha_composite(im, (int((p['x'] + 0.5) * T - im.size[0] / 2),
                                    int((p['y'] + 1) * T + p.get('dy', 0) - im.size[1])))

    # --- walls --------------------------------------------------------------
    solid = build_solid(m)
    for ty in range(m['rows']):
        for tx in range(m['cols']):
            if not solid[ty][tx]:
                continue
            if ty == 0:
                name = 'wall_top'
            elif ty == 1:
                name = 'wall_mid'
            elif ty == 2:
                name = 'wall_base'
            else:
                name = 'wall_side_base' if ty == m['rows'] - 1 else 'wall_side'
            canvas.alpha_composite(sprite(name), (tx * T, ty * T))

    # --- depth-sorted props + agents ---------------------------------------
    items = []
    for p in m['props']:
        if p.get('layer') == 'floor':
            continue
        if p['sprite'] not in frames:
            print(f"  !! unknown sprite {p['sprite']}")
            continue
        items.append(((p['y'] + 1) * T + p.get('z', 0) * 0.01, 'prop', p))
    for aid, seat in m['seats'].items():
        items.append((seat['y'] * T, 'agent', (aid, seat)))
    for d in m.get('doors', []):
        # `kind` lets a door name its own leaf family -- kept in step with the
        # same branch in canvas.js drawProps().
        base = d.get('kind') or ('door_slide_v' if d.get('v') else 'door_slide')
        name = f'{base}_0'
        if name in frames:
            items.append(((d['y'] + 1) * T, 'door', (name, d)))
    items.sort(key=lambda it: it[0])

    for _, kind, data in items:
        if kind == 'prop':
            im = sprite(data['sprite'])
            px = int((data['x'] + 0.5) * T - im.size[0] / 2)
            py = int((data['y'] + 1) * T + data.get('dy', 0) - im.size[1])
            canvas.alpha_composite(im, (px, py))
        elif kind == 'door':
            name, d = data
            im = sprite(name)
            cx = (d['x'] + 0.5) * T if d.get('v') else (d['x'] + 1) * T
            px = int(cx - im.size[0] / 2)
            py = int((d['y'] + 1) * T + d.get('dy', 0) - im.size[1])
            canvas.alpha_composite(im, (px, py))
        else:
            aid, seat = data
            key = m['agentSprites'][aid]
            clip = chars[key][f"idle_{seat['facing']}"][0]
            im = sprite(clip)
            px = int(seat['x'] * T - im.size[0] / 2)
            py = int(seat['y'] * T - im.size[1])
            canvas.alpha_composite(im, (px, py))

    # --- room shell ---------------------------------------------------------
    # Mirrors canvas.js drawShellRim/buildShell: rim strokes go down first and
    # the world, masked to the same shape, covers everything that falls inside.
    pad = SHELL_RIM + SHELL_EDGE_W * 2 + 4
    OW, OH = W + pad * 2, H + pad * 2
    out = Image.new('RGBA', (OW, OH), (0, 0, 0, 255))
    rim = ImageDraw.Draw(out)
    mask = Image.new('L', (OW, OH), 0)
    md = ImageDraw.Draw(mask)

    def blocks_px():
        for b in m['blocks']:
            yield (b['x'] * T + pad, b['y'] * T + pad,
                   (b['x'] + b['w']) * T + pad - 1, (b['y'] + b['h']) * T + pad - 1)

    # A canvas stroke straddles its path; PIL's `outline` sits entirely inside
    # it. To get the same result the rim is drawn as two filled rounded rects
    # grown outward from each block, darkest first.
    for grow, colour in ((SHELL_RIM + SHELL_EDGE_W * 2, SHELL_EDGE),
                         (SHELL_RIM, SHELL_RIM_COLOR)):
        for x0, y0, x1, y1 in blocks_px():
            rim.rounded_rectangle([x0 - grow, y0 - grow, x1 + grow, y1 + grow],
                                  radius=SHELL_RADIUS + grow, fill=colour)

    for box in blocks_px():
        md.rounded_rectangle(box, radius=SHELL_RADIUS, fill=255)
    for d_ in m['doorways']:
        md.rectangle([d_['x'] * T + pad, d_['y'] * T + pad,
                      (d_['x'] + d_.get('w', 1)) * T + pad - 1,
                      (d_['y'] + d_.get('h', 1)) * T + pad - 1], fill=255)

    world = Image.new('RGBA', (OW, OH), (0, 0, 0, 0))
    world.alpha_composite(canvas, (pad, pad))
    out.paste(world, (0, 0), Image.composite(world.split()[3], Image.new('L', (OW, OH), 0), mask))

    # --- name plates --------------------------------------------------------
    # canvas.js draws these at screen resolution, so the preview does too:
    # upscale the pixel art first, then set type on top of it.
    S = 2
    big = out.convert('RGB').resize((OW * S, OH * S), Image.NEAREST)
    bd = ImageDraw.Draw(big)
    fs = 13
    try:
        font = ImageFont.truetype('consola.ttf', fs)
    except OSError:
        font = ImageFont.load_default()

    # The in-world plate is gone from canvas.js -- a bubble now pops only when
    # an agent is handed work -- so the preview no longer draws one either.
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'office_preview.png')
    out.convert('RGB').save(path)
    big.save(os.path.join(OUT, 'office_preview_2x.png'))
    print(f'wrote {path}  ({OW}x{OH})')


if __name__ == '__main__':
    main()
