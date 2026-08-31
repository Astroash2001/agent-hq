"""
Auto-slice the source spritesheets into individual sprites.

Hand-reading tile coordinates off a spritesheet is how you end up with
half-sprites (see the 32x32-vs-16x32 character bug). Instead we find connected
islands of opaque pixels and report their real bounding boxes, then render an
indexed contact sheet so each piece can be named against a picture.

    python tools/slice_atlas.py

Writes sprite_index.json plus sprite_index_<sheet>.png preview sheets into
tools/_slices/.
"""
import json
import os
from collections import deque

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_slices')

SHEETS = {
    'littlebits_objects': r'Little_Bits_Office_tileset\Office tiles\Little_Bits_office_objects.png',
    'littlebits_walls':   r'Little_Bits_Office_tileset\Office tiles\Little_Bits_office_walls.png',
    'littlebits_floors':  r'Little_Bits_Office_tileset\Office tiles\Little_Bits_Office_Floors.png',
    'pixellife':          r'Pixel Life - Desk Essentials\spritesheet.png',
    'tdr_furniture1':     r'furniture\Top-Down_Retro_Interior\TopDownHouse_FurnitureState1.png',
    'tdr_furniture2':     r'furniture\Top-Down_Retro_Interior\TopDownHouse_FurnitureState2.png',
    'tdr_small':          r'furniture\Top-Down_Retro_Interior\TopDownHouse_SmallItems.png',
}

ALPHA_MIN = 8
MIN_AREA = 12          # ignore stray specks
GAP = 1                # islands touching diagonally count as one sprite


def find_islands(img):
    """4/8-connected flood fill over opaque pixels; returns bounding boxes."""
    w, h = img.size
    px = img.load()
    seen = bytearray(w * h)
    boxes = []

    for sy in range(h):
        for sx in range(w):
            if seen[sy * w + sx] or px[sx, sy][3] < ALPHA_MIN:
                continue
            q = deque([(sx, sy)])
            seen[sy * w + sx] = 1
            x0 = x1 = sx
            y0 = y1 = sy
            area = 0
            while q:
                x, y = q.popleft()
                area += 1
                if x < x0: x0 = x
                if x > x1: x1 = x
                if y < y0: y0 = y
                if y > y1: y1 = y
                for dy in range(-GAP, GAP + 1):
                    for dx in range(-GAP, GAP + 1):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] \
                                and px[nx, ny][3] >= ALPHA_MIN:
                            seen[ny * w + nx] = 1
                            q.append((nx, ny))
            if area >= MIN_AREA:
                boxes.append({'x': x0, 'y': y0, 'w': x1 - x0 + 1, 'h': y1 - y0 + 1, 'area': area})

    boxes.sort(key=lambda b: (b['y'] // 8, b['x']))
    return boxes


def preview(img, boxes, path, scale=6):
    cols = 8
    cell = 130
    rows = (len(boxes) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * cell, max(1, rows) * cell), (34, 30, 36))
    d = ImageDraw.Draw(sheet)
    for i, b in enumerate(boxes):
        cx, cy = (i % cols) * cell, (i // cols) * cell
        crop = img.crop((b['x'], b['y'], b['x'] + b['w'], b['y'] + b['h']))
        s = min((cell - 34) / b['w'], (cell - 34) / b['h'], scale)
        big = crop.resize((max(1, int(b['w'] * s)), max(1, int(b['h'] * s))), Image.NEAREST)
        bg = Image.new('RGBA', big.size, (74, 70, 78, 255))
        bg.alpha_composite(big)
        sheet.paste(bg.convert('RGB'),
                    (cx + (cell - big.size[0]) // 2, cy + 26 + (cell - 34 - big.size[1]) // 2))
        d.text((cx + 5, cy + 4), f"[{i}]", fill=(250, 200, 110))
        d.text((cx + 34, cy + 4), f"{b['x']},{b['y']} {b['w']}x{b['h']}", fill=(150, 145, 150))
    sheet.save(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    index = {}
    for name, rel in SHEETS.items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f'  !! missing {rel}')
            continue
        img = Image.open(path).convert('RGBA')
        boxes = find_islands(img)
        index[name] = {'source': rel.replace('\\', '/'), 'size': list(img.size), 'sprites': boxes}
        preview(img, boxes, os.path.join(OUT, f'sprite_index_{name}.png'))
        print(f'{name:<22} {img.size[0]:>4}x{img.size[1]:<4}  {len(boxes):>3} sprites')

    with open(os.path.join(OUT, 'sprite_index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)
    print(f'\nwrote {os.path.join(OUT, "sprite_index.json")}')


if __name__ == '__main__':
    main()
