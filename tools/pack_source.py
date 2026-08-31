"""
Resolve Modern Interiors sprites by (sheet, index).

build_atlas.py used to name sprites by raw pixel rectangle, which is how the
atlas ended up with clipped sofas and bookshelves missing their top shelf. The
index built by tools/index_pack.py already knows every sprite's exact bounding
box, so a pick here is a sheet name and a number off the contact sheet and the
box comes from the index -- there is no coordinate to get wrong.

    from tools.pack_source import pick
    img = pick('1_Generic', 35)          # the green rug

Run tools/index_pack.py for a sheet before picking from it.
"""
import json
import os
import functools

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_index')


@functools.lru_cache(maxsize=None)
def _index(sheet):
    path = os.path.join(INDEX, f'{sheet}.json')
    if not os.path.exists(path):
        raise FileNotFoundError(
            f'no index for {sheet!r} -- run: python tools/index_pack.py {sheet}')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


@functools.lru_cache(maxsize=None)
def _sheet_image(path):
    return Image.open(os.path.join(ROOT, path)).convert('RGBA')


def trim(im):
    """Crop transparent padding.

    The Modern Office singles are every one of them padded into a uniform
    32x48 frame, so an untrimmed sprite carries up to 40 rows of empty pixels.
    The renderer anchors bottom-centre, so that padding would push each prop
    off its own baseline. Sheet crops are already tight; trimming them is a
    no-op.
    """
    box = im.getbbox()
    return im if box is None else im.crop(box)


def pick(sheet, i):
    """One sprite, cropped to its own bounds. Returns a fresh RGBA image."""
    idx = _index(sheet)
    try:
        entry = idx['sprites'][i]
    except IndexError:
        raise IndexError(
            f'{sheet}: index {i} out of range (0..{len(idx["sprites"]) - 1})')

    if idx['kind'] == 'folder':
        return trim(Image.open(os.path.join(ROOT, idx['source'], entry['file'])
                               ).convert('RGBA'))

    x, y, w, h = entry['box']
    return _sheet_image(idx['source']).crop((x, y, x + w, y + h)).copy()


def size(sheet, i):
    e = _index(sheet)['sprites'][i]
    return e['w'], e['h']


def count(sheet):
    return len(_index(sheet)['sprites'])
