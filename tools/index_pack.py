"""
Build a browsable index of the Modern Interiors pack.

Picking sprites by eyeballing pixel coordinates off a 256x1248 sheet is how the
old atlas ended up with half-clipped sofas. This does it the other way round:
every sprite gets segmented out, numbered, and laid onto a contact sheet, so a
sprite is chosen by index and its exact box comes from the index file.

Two kinds of source:

  * a Theme_Sorter sheet  -> connected-component segmentation gives one entry
                             per sprite (the pack leaves >=1px of transparent
                             gutter between them)
  * a Theme_Sorter_Singles folder -> one entry per file, already cropped

    python tools/index_pack.py 1_Generic
    python tools/index_pack.py 12_Kitchen_Singles
    python tools/index_pack.py --list

Writes tools/_index/<name>.json and tools/_index/<name>.png
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from pack_source import trim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK = os.path.join(ROOT, 'furniture', 'final', 'moderninteriors',
                    '1_Interiors', '16x16')
SHEETS = os.path.join(PACK, 'Theme_Sorter')
SINGLES = os.path.join(PACK, 'Theme_Sorter_Singles')
SUBFILES = os.path.join(PACK, 'Room_Builder_subfiles')

# Modern Office - Revamped. Separate purchase, separate tree, same 16x16 grid
# and the same artist, so it composites with Modern Interiors directly.
OFFICE = os.path.join(ROOT, 'furniture', 'final', 'Modern_Office_Revamped_v1.2')
OFFICE_SINGLES = os.path.join(OFFICE, '4_Modern_Office_singles', '16x16')
OFFICE_ROOM = os.path.join(OFFICE, '1_Room_Builder_Office')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_index')

# Contact-sheet layout. Every sprite is rescaled so its longest side is FIT_PX:
# sizing cells to the largest sprite instead lets one 96x64 rug shrink every mug
# in the theme to a single unreadable pixel.
CELL_PAD = 4
LABEL_H = 9
FIT_PX = 52
COLS_TARGET = 1500
MIN_AREA = 12          # drop stray single pixels, keep small props like mugs


def sources():
    """Every indexable source, keyed by the name passed on the command line."""
    out = {}
    for d, tag in ((SHEETS, ''), (SUBFILES, '')):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith('.png'):
                out[fn[:-4].replace('_16x16', '')] = ('sheet', os.path.join(d, fn))
    if os.path.isdir(OFFICE_SINGLES):
        out['Office_Singles'] = ('folder', OFFICE_SINGLES)
    if os.path.isdir(OFFICE_ROOM):
        for fn in sorted(os.listdir(OFFICE_ROOM)):
            if fn.endswith('16x16.png'):
                out['Office_' + fn[:-10].rstrip('_')] = ('sheet',
                                                         os.path.join(OFFICE_ROOM, fn))
    if os.path.isdir(SINGLES):
        for sub in sorted(os.listdir(SINGLES)):
            p = os.path.join(SINGLES, sub)
            if os.path.isdir(p):
                out[sub] = ('folder', p)
    return out


def segment(path):
    """Split a sheet into sprites. Returns [(box, image), ...] in reading order."""
    im = Image.open(path).convert('RGBA')
    alpha = np.array(im)[:, :, 3] > 8
    lab, _ = ndimage.label(alpha, structure=np.ones((3, 3)))
    items = []
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        if (x1 - x0) * (y1 - y0) < MIN_AREA:
            continue
        items.append(((x0, y0, x1 - x0, y1 - y0), im.crop((x0, y0, x1, y1))))
    # Reading order, banded by row so a tall sprite does not reorder its neighbours.
    items.sort(key=lambda it: (it[0][1] // 24, it[0][0]))
    return items


def load_folder(path):
    items = []
    for fn in sorted(os.listdir(path), key=lambda s: (len(s), s)):
        if not fn.endswith('.png'):
            continue
        # Same trim as pack_source.pick, so the index reports the size the
        # atlas will actually get.
        im = trim(Image.open(os.path.join(path, fn)).convert('RGBA'))
        items.append(((fn, 0, im.width, im.height), im))
    return items


def fit(im):
    """Scale so the longest side is FIT_PX. Integer factors only, so pixel art
    stays on its own grid rather than shimmering."""
    longest = max(im.width, im.height)
    if longest > FIT_PX:
        k = -(-longest // FIT_PX)                      # ceil, downscale by 1/k
        return im.resize((max(1, im.width // k), max(1, im.height // k)),
                         Image.NEAREST)
    k = max(1, FIT_PX // longest)
    return im.resize((im.width * k, im.height * k), Image.NEAREST)


def contact_sheet(items, title):
    """Numbered grid, one cell per sprite, every sprite scaled to a common size."""
    thumbs = [fit(im) for _, im in items]
    cw = FIT_PX + CELL_PAD * 2
    ch = FIT_PX + CELL_PAD * 2 + LABEL_H
    cols = max(1, COLS_TARGET // cw)
    rows = (len(thumbs) + cols - 1) // cols

    sheet = Image.new('RGB', (cols * cw, rows * ch + 14), (22, 20, 26))
    d = ImageDraw.Draw(sheet)
    d.text((4, 3), f'{title}  -  {len(items)} sprites', fill=(238, 176, 99))

    for i, (t, (_, orig)) in enumerate(zip(thumbs, items)):
        cx = (i % cols) * cw
        cy = (i // cols) * ch + 14
        d.rectangle([cx, cy, cx + cw - 1, cy + ch - 1], outline=(52, 46, 56))
        sheet.paste(t, (cx + (cw - t.width) // 2,
                        cy + LABEL_H + (ch - LABEL_H - t.height) // 2), t)
        d.text((cx + 2, cy + 1), f'{i}', fill=(150, 190, 150))
        # Real dimensions matter when picking: a 46x47 bookshelf and a 12x29 one
        # look identical once both are fitted to 52px.
        d.text((cx + cw - 30, cy + 1), f'{orig.width}x{orig.height}',
               fill=(110, 104, 116))
    return sheet


def main():
    src = sources()
    if len(sys.argv) < 2 or sys.argv[1] == '--list':
        for k, (kind, p) in sorted(src.items()):
            print(f'{kind:7s}  {k}')
        return

    name = sys.argv[1]
    if name not in src:
        matches = [k for k in src if name.lower() in k.lower()]
        if len(matches) != 1:
            sys.exit(f'unknown source {name!r}; try --list' +
                     (f'\ndid you mean: {matches}' if matches else ''))
        name = matches[0]

    kind, path = src[name]
    items = segment(path) if kind == 'sheet' else load_folder(path)
    if not items:
        sys.exit(f'{name}: nothing found')

    os.makedirs(OUT, exist_ok=True)
    index = {
        'source': os.path.relpath(path, ROOT).replace('\\', '/'),
        'kind': kind,
        'sprites': [
            {'i': i, 'box': list(box) if kind == 'sheet' else None,
             'file': box[0] if kind == 'folder' else None,
             'w': im.width, 'h': im.height}
            for i, (box, im) in enumerate(items)
        ],
    }
    with open(os.path.join(OUT, f'{name}.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=1)
    contact_sheet(items, name).save(os.path.join(OUT, f'{name}.png'))
    print(f'{name}: {len(items)} sprites -> tools/_index/{name}.json + .png')


if __name__ == '__main__':
    main()
