"""
Generate the floor tiles for the office map.

The reference art (final.jpg) is an AI-generated concept image, so its floor is
not on the 16px grid the Modern Interiors atlas uses -- measured plank pitch is
~11.3px. Nothing in Room_Builder_free matches it structurally either: the atlas
wood floors carry vertical grain ticks, the reference planks are smooth.

So we synthesise the floor instead of force-fitting an atlas tile. The block is
16x48: three 16px tiles tall, holding four 12px planks, so it repeats cleanly
against the tile grid every 3 rows.

Colours below were sampled from clean floor patches of the reference (four
independent patches agreed within 4/255).

    python tools/gen_tiles.py

Requires Pillow. Output is committed, so this only needs re-running if the
palette or plank rhythm changes.
"""
import os

from PIL import Image

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'frontend', 'assets', 'generated')

TILE = 16
BLOCK_H = 48
PLANKS = 4
PITCH = BLOCK_H // PLANKS          # 12px, matches the measured 11.3px

PLANK_BODY = (161, 91, 62)
PLANK_SEAM = (129, 57, 39)
SHADOW_MUL = 0.94
GRAIN = 3                          # +/- jitter, keeps the fill from reading as flat colour


def _noise(seed):
    """Deterministic per-pixel jitter -- same tile every run, no flicker."""
    return ((seed * 1103515245 + 12345) >> 16) % (GRAIN * 2 + 1) - GRAIN


def _clamp(v):
    return max(0, min(255, int(round(v))))


# Floor stays synthesised. The purchased Room Builder floors were scored against
# the reference's measured (168,104,62): the closest plank block is at (128,192)
# in Room_Builder_Floors, but it is markedly more saturated and its pattern is
# busier than the reference's quiet planks. Measured, the synthesised block is
# the better match, so the pack floors are deliberately not used.
def plank_block(width=TILE, height=BLOCK_H, stagger=0):
    """One seamless floor block. `stagger` shifts the butt-joint so variants differ."""
    img = Image.new('RGBA', (width, height))
    px = img.load()
    seams = {(i * PITCH) % height for i in range(PLANKS)}

    for y in range(height):
        if y in seams:
            base = PLANK_SEAM
        elif (y - 1) % PITCH == 0:
            base = tuple(c * SHADOW_MUL for c in PLANK_BODY)
        else:
            base = PLANK_BODY

        for x in range(width):
            n = _noise(x * 31 + y * 17 + stagger * 7919)
            px[x, y] = (_clamp(base[0] + n), _clamp(base[1] + n), _clamp(base[2] + n), 255)

    # Vertical butt-joint, offset per variant so a tiled field never shows a seam column.
    if stagger:
        jx = (stagger * 5) % width
        for y in range(height):
            if y in seams:
                continue
            r, g, b, a = px[jx, y]
            px[jx, y] = (_clamp(r * 0.88), _clamp(g * 0.88), _clamp(b * 0.88), a)

    return img


# ---------------------------------------------------------------------------
# Hand-authored props
#
# The water cooler, coffee machine and area rug appear in the reference but do
# not exist in ANY of the source packs. They are drawn here from pixel maps in
# the established palette: hard near-black outline (matching the reference's
# 18.7% outline weight) over warm mid-tones.
#
# Each map is a list of strings; one character per pixel, keyed by PALETTE.
# '.' is transparent.
# ---------------------------------------------------------------------------

PALETTE = {
    '.': None,
    'k': (26, 18, 20),        # outline
    'd': (74, 52, 44),        # deep shadow
    'w': (232, 226, 210),     # white plastic
    'W': (255, 252, 244),     # white highlight
    'b': (108, 164, 196),     # water blue
    'B': (150, 200, 226),     # water highlight
    'g': (120, 112, 106),     # grey metal
    'G': (168, 160, 150),     # grey highlight
    's': (150, 92, 58),       # warm wood / body
    'S': (188, 126, 80),      # warm highlight
    'r': (138, 46, 44),       # deep red
    'R': (176, 66, 58),       # red highlight
    'n': (46, 38, 40),        # dark machine body
    'o': (214, 148, 74),      # amber accent
    'e': (86, 130, 96),       # green rug
    'E': (110, 158, 118),     # green rug light
}

WATER_COOLER = [
    '...kkkkkkkk...',
    '..kbbbbbbbbk..',
    '.kbBBbbbbbbbk.',
    '.kbBbbbbbbbbk.',
    '.kbbbbbbbbbbk.',
    '..kbbbbbbbbk..',
    '...kwwwwwwk...',
    '..kwWwwwwwwk..',
    '..kwwwwwwwwk..',
    '..kwwkkkkwwk..',
    '..kwwkggkwwk..',
    '..kwwkkkkwwk..',
    '..kwwwwwwwwk..',
    '..kwwwwwwwwk..',
    '..kwwwwwwwwk..',
    '..kwWwwwwwwk..',
    '..kwwwwwwwwk..',
    '..kddddddddk..',
    '..kkkkkkkkkk..',
]

COFFEE_MACHINE = [
    '.kkkkkkkkkkkk.',
    'kngggggggggnk',
    'kngGGgggggggnk',
    'kngggggggggnk',
    'knnnnnnnnnnnk',
    'knwwwwwwwwwnk',
    'knwkkkkkkkwnk',
    'knwknnnnnkwnk',
    'knwknoooonkwnk',
    'knwknnnnnkwnk',
    'knwkkkkkkkwnk',
    'knwwwwwwwwwnk',
    'knnnnnnnnnnnk',
    'kngggggggggnk',
    'kndddddddddnk',
    '.kkkkkkkkkkk.',
]

PRINTER_SMALL = [
    '.kkkkkkkkkkkkkk.',
    'kgGGggggggggggk',
    'kgggggggggggggk',
    'kkkkkkkkkkkkkkk',
    'kwwwwwwwwwwwwwk',
    'kwkkkkkkkkkkkwk',
    'kwkgggggggggkwk',
    'kwkkkkkkkkkkkwk',
    'kwwwwwwwwwwwwwk',
    'kgggggggggggggk',
    'kdddddddddddddk',
    '.kkkkkkkkkkkkk.',
]


def from_map(rows):
    """Build an RGBA sprite from a character map."""
    h = len(rows)
    w = max(len(r) for r in rows)
    img = Image.new('RGBA', (w, h))
    px = img.load()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            col = PALETTE.get(ch)
            if col:
                px[x, y] = (col[0], col[1], col[2], 255)
    return img


def area_rug(w=96, h=64):
    """Green executive rug with a bordered inlay, as in the reference."""
    img = Image.new('RGBA', (w, h))
    px = img.load()
    base, light = PALETTE['e'], PALETTE['E']
    edge = PALETTE['d']
    for y in range(h):
        for x in range(w):
            on_edge = x < 2 or y < 2 or x >= w - 2 or y >= h - 2
            inlay = (4 <= x < w - 4 and 4 <= y < h - 4 and
                     (x in (4, 5, w - 5, w - 6) or y in (4, 5, h - 5, h - 6)))
            if on_edge:
                c = edge
            elif inlay:
                c = light
            else:
                c = base
            n = _noise(x * 13 + y * 29) // 2
            px[x, y] = (_clamp(c[0] + n), _clamp(c[1] + n), _clamp(c[2] + n), 255)
    return img


# Wall-mounted widescreen TV. None of the packs ship a usable top-down one:
# the LPC TV is 59% black outline and reads as a different game entirely.
TV_WIDESCREEN = [
    'kkkkkkkkkkkkkkkkkkkkkkkk',
    'knnnnnnnnnnnnnnnnnnnnnnk',
    'knbbbbbbbbbbbbbbbbbbbbnk',
    'knbBBBBbbbbbbbbbbbbbbbnk',
    'knbBoooobbbbbbbbbbbbbbnk',
    'knbbooooobbbeeebbbbbbbnk',
    'knbbbbbbbbbeeeeebbbbbbnk',
    'knbbbbbbbbbbeeebbbbbbbnk',
    'knbbbbbbbbbbbbbbbbbBBbnk',
    'knbbbbbbbbbbbbbbbbbbbbnk',
    'knnnnnnnnnnnnnnnnnnnnnnk',
    'kkkkkgggggggggggggkkkkkk',
    '....kkkkkkkkkkkkkkk.....',
]


# ---------------------------------------------------------------------------
# L-shaped workstation + office chair
#
# No pack has either. Colours below are sampled from the Pixel Life desk so the
# authored pieces sit in the same palette as the rest of the office:
#   top #ead4aa   outline #3f2832   apron #b86f50   highlight #e4a672
#
# Construction mirrors that desk's row structure: 1px dark outline, a flat top
# surface, a dark front edge, an apron band with a highlight line, then legs.
# ---------------------------------------------------------------------------

# Dark-brown employee desks. The Pixel Life desk these were derived from is a
# light tan (#ead4aa top); against the floor at (163,97,61) that read as pale
# and washed out. These values keep the same 4-tone structure but sit clearly
# darker than the floor, so the desks read as solid wood.
DESK_TOP    = (118,  78,  52)
DESK_EDGE   = ( 40,  26,  30)
DESK_APRON  = ( 84,  54,  38)
DESK_HILITE = (150, 102,  66)


def _slab(w, h, surface_h, apron_h, leg_xs, leg_h, top_outline=True):
    """One desk slab: outline, flat top, front edge, apron with a highlight
    line, then legs -- the same row structure as the Pixel Life desk."""
    img = Image.new('RGBA', (w, h))
    px = img.load()

    def fill(x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(h, y1)):
            for x in range(max(0, x0), min(w, x1)):
                px[x, y] = (c[0], c[1], c[2], 255)

    y = 0
    if top_outline:
        fill(0, 0, w, 1, DESK_EDGE)
        y = 1
    fill(0, y, w, surface_h, DESK_TOP)
    fill(0, surface_h, w, surface_h + 1, DESK_EDGE)
    apron_top = surface_h + 1
    fill(0, apron_top, w, apron_top + apron_h, DESK_APRON)
    fill(0, apron_top + apron_h - 2, w, apron_top + apron_h - 1, DESK_HILITE)
    fill(0, apron_top + apron_h, w, apron_top + apron_h + 1, DESK_EDGE)
    leg_top = apron_top + apron_h + 1
    for lx in leg_xs:
        fill(lx, leg_top, lx + 4, min(h, leg_top + leg_h), DESK_EDGE)
    return img


# The L is TWO sprites, not one. A single 48x48 sprite puts its baseline at the
# bottom of the wing, which is below where the occupant stands -- the desk would
# then always sort in front of the agent and hide them. Splitting the shape lets
# each half sort independently: the agent draws after the main run (in front of
# it) while the wing sits off to the right and never overlaps them.
def l_desk_main():
    """Back run of the L: 48x31."""
    return _slab(48, 31, surface_h=15, apron_h=5, leg_xs=(2, 42), leg_h=8)


def l_desk_wing():
    """Return wing of the L: 18x32, sits at the right end one row lower."""
    return _slab(18, 32, surface_h=16, apron_h=5, leg_xs=(2, 12), leg_h=8,
                 top_outline=False)


def l_desk_wide():
    """Executive variant of the back run: 64x31 instead of 48."""
    return _slab(64, 31, surface_h=15, apron_h=5, leg_xs=(2, 58), leg_h=8)


DESK_PEDESTAL = [
    'kkkkkkkkkkkkkkkk',
    'khhhhhhhhhhhhhhk',
    'kttttttttttttttk',
    'ktkkkkkkkkkkkktk',
    'ktkddddddddddktk',
    'ktkdddddwwddd ktk',
    'ktkkkkkkkkkkkktk',
    'ktttttttttttttkk',
    'ktkkkkkkkkkkkktk',
    'ktkddddddddddktk',
    'ktkdddddwwdddktk',
    'ktkkkkkkkkkkkktk',
    'ktttttttttttttkk',
    'ktkkkkkkkkkkkktk',
    'ktkddddddddddktk',
    'ktkdddddwwdddktk',
    'ktkkkkkkkkkkkktk',
    'kttttttttttttttk',
    'kaaaaaaaaaaaaaak',
    'kkkkkkkkkkkkkkkk',
]


def desk_pedestal():
    """Three-drawer cabinet that tucks under the wide desks, same palette."""
    pal = dict(PALETTE)
    pal.update({
        'k': DESK_EDGE,
        'h': DESK_HILITE,
        't': DESK_TOP,
        'd': DESK_APRON,
        'a': (62, 40, 30),
        'w': (176, 128, 84),      # drawer pull
    })
    h = len(DESK_PEDESTAL)
    w = max(len(r) for r in DESK_PEDESTAL)
    img = Image.new('RGBA', (w, h))
    px = img.load()
    for y, row in enumerate(DESK_PEDESTAL):
        for x, ch in enumerate(row):
            c = pal.get(ch)
            if c:
                px[x, y] = (c[0], c[1], c[2], 255)
    return img


CHAIR_OFFICE = [
    '....kkkkkkkk....',
    '..kkbbbbbbbbkk..',
    '.kbbBBBBBBBBbbk.',
    '.kbBBbbbbbbBBbk.',
    '.kbBbbbbbbbbBbk.',
    '.kbBbbbbbbbbBbk.',
    '.kbBbbbbbbbbBbk.',
    '.kbbbbbbbbbbbbk.',
    '..kbbbbbbbbbbk..',
    '..kkkkkkkkkkkk..',
    '...kaaaaaaaak...',
    '...kaAAAAAAak...',
    '...kaaaaaaaak...',
    '....kkkkkkkk....',
    '......kddk......',
    '......kddk......',
    '.....kkddkk.....',
    '..kkkkddddkkkk..',
    '.kddkkddddkkddk.',
    'kddk..kddk..kddk',
    'kdk....kk....kdk',
    '.k...........k..',
]


def office_chair():
    """Swivel chair at 16x22 -- the Little Bits chair is only 10x16, which reads
    as undersized against a 16x32 character."""
    pal = dict(PALETTE)
    pal.update({
        'b': (150,  92,  58),      # backrest
        'B': (190, 128,  86),      # backrest highlight
        'a': (168, 106,  66),      # seat
        'A': (206, 146,  98),      # seat highlight
        'd': ( 74,  60,  62),      # post + star base
    })
    h = len(CHAIR_OFFICE)
    w = max(len(r) for r in CHAIR_OFFICE)
    img = Image.new('RGBA', (w, h))
    px = img.load()
    for y, row in enumerate(CHAIR_OFFICE):
        for x, ch in enumerate(row):
            c = pal.get(ch)
            if c:
                px[x, y] = (c[0], c[1], c[2], 255)
    return img


# ---------------------------------------------------------------------------
# Conference room pieces
# ---------------------------------------------------------------------------

# tables0_extracted/layer1.png holds a 31x14 long-table unit at (120,1) whose
# left and right edges match, so repeating it produces a seamless boardroom
# table with legs appearing at each join. Measured style distance from the
# reference is ~11 -- the closest of any asset in the project.
TABLES_SHEET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'furniture', 'tables0_extracted', 'layer1.png')
TABLE_UNIT = (120, 1, 31, 14)
TABLE_REPEATS = 3


def conference_table():
    src = Image.open(TABLES_SHEET).convert('RGBA')
    x, y, w, h = TABLE_UNIT
    unit = src.crop((x, y, x + w, y + h))
    out = Image.new('RGBA', (w * TABLE_REPEATS, h))
    for i in range(TABLE_REPEATS):
        out.paste(unit, (i * w, 0))
    return out


def projector_screen(w=48, h=14):
    """Pull-down presentation screen for the conference wall.

    Height is capped at 14 so the whole sprite fits inside the south wall's
    16px band. At 20 it hung below the wall and read as falling out of the
    office.
    """
    housing = (52, 40, 44)
    frame = (86, 62, 48)
    screen = (232, 228, 214)
    screen_lo = (206, 200, 186)
    img = Image.new('RGBA', (w, h))
    px = img.load()

    def fill(x0, y0, x1, y1, c):
        for yy in range(max(0, y0), min(h, y1)):
            for xx in range(max(0, x0), min(w, x1)):
                px[xx, yy] = (c[0], c[1], c[2], 255)

    fill(0, 0, w, 3, housing)              # roller housing
    fill(0, 3, w, 4, frame)
    fill(0, 4, w, h - 2, screen)
    for yy in range(4, h - 2):             # faint sheen down the screen
        if (yy - 4) % 4 == 3:
            fill(1, yy, w - 1, yy + 1, screen_lo)
    fill(0, h - 2, w, h - 1, frame)
    fill(0, h - 1, w, h, housing)
    # side rails
    fill(0, 3, 1, h, frame)
    fill(w - 1, 3, w, h, frame)
    return img


def counter_sink_vertical():
    """Vertical kitchen sink counter running along the right (east) wall."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tdr1_path = os.path.join(root, 'furniture', 'Top-Down_Retro_Interior', 'TopDownHouse_FurnitureState1.png')
    tdr1 = Image.open(tdr1_path).convert('RGBA')
    # Crop vertical counter run from TDR1 (x=192..208, y=192..250 -> 16x58)
    v_run = tdr1.crop((192, 192, 208, 250))
    pix = v_run.load()

    # Draw vertical sink basin & faucet (facing inward into room)
    rim_color = (68, 43, 56, 255)
    rim_hi = (109, 72, 85, 255)
    basin_dark = (52, 35, 45, 255)
    basin_mid = (76, 52, 65, 255)
    faucet_base = (130, 95, 110, 255)
    faucet_hi = (175, 135, 150, 255)

    for y in range(16, 38):
        for x in range(2, 14):
            if x in (2, 13) or y in (16, 37):
                pix[x, y] = rim_color
            else:
                pix[x, y] = basin_mid

    for y in range(17, 37):
        for x in range(3, 13):
            if x == 3 or y == 17:
                pix[x, y] = basin_dark
            elif x == 12 or y == 36:
                pix[x, y] = rim_hi

    pix[12, 25] = faucet_base
    pix[12, 26] = faucet_hi
    pix[12, 27] = faucet_base
    pix[11, 26] = faucet_hi
    pix[10, 26] = faucet_base

    return v_run


def planter_run(w=64):
    """Long planter trough of dense foliage -- the runs between desk ranks.

    Neither pack has one: Modern Interiors has potted plants, Modern Office has
    desk greenery, but the low wooden box packed with hedge is unique to the
    reference. Built rather than substituted because it is the thing that
    separates the desk ranks visually, and a row of single pots does not read
    the same way at all.

    Foliage is scattered from a hash of x so the run is deterministic but never
    periodic -- a repeating clump reads as wallpaper at this scale.
    """
    h = 14
    box_top = 5
    rim = (120, 74, 46)
    rim_lit = (156, 100, 64)
    body = (96, 58, 36)
    body_dk = (68, 40, 26)
    soil = (52, 34, 24)
    leaf = [(58, 96, 44), (76, 122, 54), (96, 146, 66), (44, 76, 36)]

    img = Image.new('RGBA', (w, h))
    px = img.load()

    def fill(x0, y0, x1, y1, c):
        for yy in range(max(0, y0), min(h, y1)):
            for xx in range(max(0, x0), min(w, x1)):
                px[xx, yy] = (c[0], c[1], c[2], 255)

    # Trough: lit rim, body, dark base line.
    fill(0, box_top, w, box_top + 2, rim_lit)
    fill(0, box_top + 2, w, h - 2, body)
    fill(0, h - 2, w, h, body_dk)
    fill(0, box_top, 1, h, rim)
    fill(w - 1, box_top, w, h, rim)
    fill(1, box_top + 2, w - 1, box_top + 3, soil)

    # Foliage mounded above the rim.
    for x in range(1, w - 1):
        n = _noise(x * 7919)
        top = box_top - 4 - (n % 3)
        for y in range(max(0, top), box_top + 4):
            c = leaf[(x * 3 + y * 5) % len(leaf)]
            if y == top:
                c = leaf[2]
            px[x, y] = (c[0], c[1], c[2], 255)
        # Occasional taller sprig breaks the flat top edge.
        if x % 7 == 3:
            for y in range(max(0, top - 2), top):
                px[x, y] = leaf[1] + (255,)
    return img


def board_table(w=208, h=42):
    """Wide boardroom table in dark walnut.

    Both packs top out around 80px -- five tiles -- and the reference's table
    runs the length of the room with six seats a side. Rather than tile a short
    one and leave a seam every five tiles, this draws the whole slab: rounded
    ends, a lighter grain field, and the inset runner down the middle that the
    reference uses to break up the surface.
    """
    edge = (58, 34, 22)
    rail = (96, 58, 36)
    top = (122, 74, 46)
    top_lit = (146, 92, 58)
    inlay = (86, 51, 32)
    leg = (52, 31, 20)

    img = Image.new('RGBA', (w, h))
    px = img.load()
    r = 10                                   # corner radius of the rounded ends

    def inside(x, y):
        """Stadium outline: square in the middle, rounded at both ends."""
        if y < 0 or y >= h - 4:
            return False
        if x < r:
            dx, dy = r - x, 0
            cy = h / 2 - 2
            return ((r - x) / r) ** 2 + ((y - cy) / (cy)) ** 2 <= 1
        if x >= w - r:
            cy = h / 2 - 2
            return ((x - (w - r - 1)) / r) ** 2 + ((y - cy) / (cy)) ** 2 <= 1
        return True

    for y in range(h - 4):
        for x in range(w):
            if not inside(x, y):
                continue
            if y < 2 or y >= h - 6 or not inside(x - 1, y) or not inside(x + 1, y):
                c = edge
            elif y < 4:
                c = top_lit
            elif 12 <= y <= h - 14 and r + 4 < x < w - r - 4:
                c = inlay
            else:
                c = top
            n = _noise(x * 31 + y * 17)
            px[x, y] = (_clamp(c[0] + n), _clamp(c[1] + n), _clamp(c[2] + n), 255)

    # Apron and legs.
    for x in range(r, w - r):
        for y in range(h - 6, h - 3):
            px[x, y] = rail + (255,)
    for lx in (r + 6, w // 2 - 3, w - r - 9):
        for x in range(lx, lx + 6):
            for y in range(h - 4, h):
                px[x, y] = leg + (255,)
    return img


def conference_rug(w=272, h=72):
    # Height is the room's actual floor depth, not a guess: the glazing's lower
    # edge sits at y=294 and the south wall starts at 368, so 72 fills the room
    # without the rug climbing out through the glass wall.
    """Large area rug under the boardroom table.

    The reference defines the meeting room with a single pale rug spanning the
    whole table and both chair ranks -- that rug is what separates the room from
    the open floor, more than the glazing does. Nothing in either pack is close
    to this size (the biggest is 56x58), so it is drawn: a woven field, a woven
    border a few pixels in, and a short fringe on the left and right ends.
    """
    field = (156, 145, 132)
    field_alt = (166, 155, 142)
    border = (127, 118, 106)
    border_lit = (176, 167, 154)
    fringe = (139, 130, 118)

    img = Image.new('RGBA', (w, h))
    px = img.load()

    def fill(x0, y0, x1, y1, c):
        for yy in range(max(0, y0), min(h, y1)):
            for xx in range(max(0, x0), min(w, x1)):
                px[xx, yy] = (c[0], c[1], c[2], 255)

    # Woven field: a fine two-tone check, jittered so it does not read as a grid.
    for y in range(h):
        for x in range(w):
            base = field if ((x >> 1) + (y >> 1)) % 2 else field_alt
            n = _noise(x * 13 + y * 7) // 2
            px[x, y] = (_clamp(base[0] + n), _clamp(base[1] + n),
                        _clamp(base[2] + n), 255)

    # Inset border, two rules with a lit edge between them.
    for inset, colour in ((4, border), (7, border_lit), (10, border)):
        fill(inset, inset, w - inset, inset + 1, colour)
        fill(inset, h - inset - 1, w - inset, h - inset, colour)
        fill(inset, inset, inset + 1, h - inset, colour)
        fill(w - inset - 1, inset, w - inset, h - inset, colour)

    # Fringe on the short ends only, the way a runner is finished.
    for y in range(6, h - 6, 3):
        fill(0, y, 4, y + 2, fringe)
        fill(w - 4, y, w, y + 2, fringe)
    return img


AUTHORED = {
    'conference_rug': conference_rug,
    'board_table': board_table,
    'planter_run': planter_run,
    'conf_table':       conference_table,
    'projector_screen': projector_screen,
    'water_cooler':     lambda: from_map(WATER_COOLER),
    'coffee_machine':   lambda: from_map(COFFEE_MACHINE),
    'printer_small':    lambda: from_map(PRINTER_SMALL),
    'tv_widescreen':    lambda: from_map(TV_WIDESCREEN),
    'desk_l':           l_desk_main,
    'desk_l_wing':      l_desk_wing,
    'desk_l_wide':      l_desk_wide,
    'desk_pedestal':    desk_pedestal,
    'chair_office':     office_chair,
    'rug_green':        area_rug,
    'counter_sink_v':   counter_sink_vertical,
}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    variants = [plank_block(stagger=s) for s in range(4)]
    sheet = Image.new('RGBA', (TILE * len(variants), BLOCK_H))
    for i, v in enumerate(variants):
        sheet.paste(v, (i * TILE, 0))

    path = os.path.join(OUT_DIR, 'floor_wood.png')
    sheet.save(path)
    print(f'wrote {path}  ({sheet.size[0]}x{sheet.size[1]}, '
          f'{len(variants)} variants of {TILE}x{BLOCK_H})')

    prop_dir = os.path.join(OUT_DIR, 'authored')
    os.makedirs(prop_dir, exist_ok=True)
    for name, build in AUTHORED.items():
        img = build()
        img.save(os.path.join(prop_dir, f'{name}.png'))
        print(f'wrote {name}.png  ({img.size[0]}x{img.size[1]})')


if __name__ == '__main__':
    main()
