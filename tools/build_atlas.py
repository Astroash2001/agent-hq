"""
Compose one unified office atlas from the mixed source packs.

Why this exists
---------------
The available art comes from three packs in three styles. Measured against the
reference (18.7% near-black outline, warmth +59, saturation 0.49):

    Pixel Life        d=30.8   <- closest; used as the style anchor
    Little Bits man   d=43.9   <- outline/sat already match, only warmth is off
    Little Bits props d=102.0  <- right outline, badly cool palette
    LimeZu interiors  d=69.4   <- soft-shaded (0.3% outline); NOT used for props

So: take Pixel Life as-is, pull the props Pixel Life lacks from Little Bits, and
warm-correct those into Pixel Life's palette. Everything lands in one atlas so
the renderer only ever binds a single texture.

    python tools/build_atlas.py
"""
import colorsys
import json
import os

from PIL import Image, ImageDraw

from pack_source import pick as pack_pick

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'frontend', 'assets', 'generated')

LB_OBJ = r'Little_Bits_Office_tileset\Office tiles\Little_Bits_office_objects.png'
PL     = r'Pixel Life - Desk Essentials\spritesheet.png'
MAN_DIR = r'Little_Bits_Office_tileset\businessman1'

# Top-Down Retro Interior. Measured against the reference it beats everything
# else available (DoorsAndWindows d=25.3, SmallItems d=36.9, Furniture d=45-53),
# and it carries the lounge/kitchen furniture the office was missing. The LPC
# office pack was rejected: right item names, wrong style (d>85, cool palettes,
# its Coffee Maker is 85% black outline).
TDR1 = r'furniture\Top-Down_Retro_Interior\TopDownHouse_FurnitureState1.png'
TDR2 = r'furniture\Top-Down_Retro_Interior\TopDownHouse_FurnitureState2.png'
TDRS = r'furniture\Top-Down_Retro_Interior\TopDownHouse_SmallItems.png'

# Indoor plants pack: 14 potted desk plants on a 64x64 sheet, 16x16 each, in
# warm terracotta pots. Purpose-built desk greenery, better than reusing the
# generic ferns. Left uncorrected -- warming would kill the foliage.
SCP = (r'furniture\sc_indoor_plants_free\sc_indoor_plants_free\Spritesheets'
       r'\plants_w_pot\sc_indoor_plants_w_pot.png')
SCA = (r'furniture\sc_indoor_plants_free\sc_indoor_plants_free\Spritesheets'
       r'\table_accessories\copper_watering_can.png')

# A single hand-supplied TV, already drawn in the orientation the conference
# room needs: screen edge-on facing west, mount hardware behind it to the east.
# It arrives as a 1024x1536 render with a soft orange bloom around the sprite
# and the brown wall it was posed against still behind it, so it is keyed and
# reduced -- see wall_tv_west().
TV_WEST = 'furniture/finaltv.png'

# Office-Furniture-Pixel-Art: individual sprite files. Quality is mixed --
# Tall-Bookshelf scores d=12 and the chairs d=36-45, but its sofas are d>200
# (near-black outlines, cold blues) and are not used.
OFP = (r'furniture\Office-Furniture-Pixel-Art\Office-Furniture-Pixel-Art'
       r'\Office-Furniture-Pixel-Art')

# Target palette, sampled from the reference art.
TARGET_WARMTH = 59.0

# name -> (source, x, y, w, h, warm_correct)
SPRITES = {
    # --- Pixel Life: the warm core, used untouched -------------------------
    'desk':         (PL,  0,  1, 32, 30, False),
    'monitor':      (PL, 103, 40, 20, 23, False),
    'keyboard':     (PL, 40, 43, 16,  9, False),
    'mouse':        (PL, 77, 44,  6,  6, False),
    'mug':          (PL, 12, 42, 10, 12, False),
    'papers':       (PL, 44, 11,  8, 11, False),
    'papers_messy': (PL, 167, 8, 20, 16, False),
    'pencil':       (PL, 78, 11,  3, 10, False),
    'sticky':       (PL, 109, 13, 6,  6, False),
    'pen_cup':      (PL, 139, 9, 10, 12, False),
    'chart_board':  (PL, 165, 39, 23, 17, False),
    'extinguisher': (PL, 139, 40, 11, 17, False),

    # --- Pixel Life walls: peach upper over brown wainscot, the same
    #     structure the reference uses. -------------------------------------
    'wall_top':     (PL, 224,  0, 16, 16, False),
    'wall_mid':     (PL, 224, 32, 16, 16, False),
    'wall_base':    (PL, 224, 48, 16, 16, False),
    'door':         (PL, 256,  0, 32, 64, False),
    'cabinet':      (PL, 192,  0, 32, 32, False),
    'floor_tile':   (PL, 192, 32, 16, 16, False),

    # --- Little Bits: fills what Pixel Life lacks, warm-corrected ----------
    'chair_a':      (LB_OBJ, 35, 32, 10, 16, True),
    'chair_b':      (LB_OBJ, 51, 32, 10, 16, True),
    'filing_cab':   (LB_OBJ, 48, 65, 16, 18, True),
    'whiteboard':   (LB_OBJ, 71, 67, 18, 24, True),
    'bench_table':  (LB_OBJ, 99, 70, 26, 19, True),
    'vending':      (LB_OBJ, 96,  2, 16, 25, True),
    'low_cabinet':  (LB_OBJ, 96, 32, 16, 12, True),
    'monitor_alt':  (LB_OBJ, 48,  5, 16, 16, True),
    'ceiling_light':(LB_OBJ, 84, 50, 11,  3, False),
    'printer':      (LB_OBJ, 64,  0, 32, 20, True),
    # The plant is already natural terracotta + green; warming it would kill the
    # foliage, so it is taken as-is.
    'plant':        (LB_OBJ, 55, 48, 18, 16, False),

    # --- Top-Down Retro Interior: lounge -------------------------------------
    'sofa_3seat':    (TDR1,  88, 161, 49, 31, True),
    'sofa_2seat':    (TDR1,  88, 161, 49, 31, True),
    'armchair_l':    (TDR1, 176, 117, 23, 36, True, False),
    'armchair_r':    (TDR1, 176, 117, 23, 36, True, True),
    'armchair_down': (TDR1, 144, 160, 32, 32, True),
    'sofa_side':     (TDR1,   0, 195, 24, 50, True),
    'coffee_table':  (TDR1,  84,  44, 24, 19, True),
    'bookshelf':     (TDR1,  33,  64, 46, 47, True),
    'bookshelf_wide':(TDR1,  97,  66, 46, 45, True),
    'fireplace':     (TDR2, 148, 112, 24, 41, True),   # State2 = lit
    'floor_lamp':    (TDR2,  97, 112, 15, 46, True),   # State2 = lit
    'clock_grand':   (TDR1,  66, 112, 26, 46, True),
    'mirror':        (TDR1, 116, 114, 24, 44, True),
    'coat_rack':     (TDR1,  49, 116, 14, 44, True),
    'side_table':    (TDR1,   2, 261, 13, 23, True),
    'dresser':       (TDR1,   4, 117, 25, 35, True),
    'round_table':   (TDR1, 176,   2, 32, 29, True),
    'plant_tall':    (TDR1,   3, 168, 10, 23, False),
    'rug_oval_sm':   (TDR1, 133,  50, 22, 12, True),
    'rug_oval_lg':   (TDR1,  99, 241, 27, 13, True),
    'rug_runner':    (TDR1, 160,  34, 48, 46, True),

    # --- Top-Down Retro Interior: kitchen ------------------------------------
    'fridge':        (TDR1,  35, 196, 26, 55, True),
    'oven':          (TDR1,  65, 190, 30, 45, True),
    # The kitchen run lives at y192-235. Everything below y=256 on this sheet is
    # the BATHROOM row (bathtub, toilets, bidet) -- an earlier crop at (128,243)
    # put two toilets in the middle of the office.
    'counter_sink':  (TDR1,  96, 192, 48, 43, True),
    'counter_run':   (TDR1, 144, 192, 48, 44, True),
    'dining_table':  (TDR1,   1,  32, 46, 30, True),
    'chair_wood':    (TDR1, 128,   4, 15, 28, True),
    'chair_wood2':   (TDR1, 144,   4, 15, 28, True),
    'chair_white':   (TDR1,  81,   4, 14, 27, True),
    'cabinet_tall':  (TDR1,  32,   3, 13, 27, True),
    'cabinet_wide':  (TDR1,  48,   5, 29, 25, True),
    'desk_ornate':   (TDR1,   6,   5, 20, 23, True),

    # --- Top-Down Retro Interior: dressing -----------------------------------
    'pic_frame_a':   (TDRS,   2,  17, 12, 14, False),
    'pic_frame_b':   (TDRS,  16,  19, 16, 11, False),
    'wall_clock':    (TDRS,  80,  16, 15, 15, False),
    'potted_flower': (TDRS,  99,  20,  8, 11, False),
    'potted_fern':   (TDRS, 113,  17, 11, 14, False),
    'books_stack':   (TDRS,  98,  35, 12, 12, True),
    'book_open':     (TDRS,   1,  71, 14,  8, False),
    'mug_tdr':       (TDRS,  17,   3, 13, 11, False),
    'teapot':        (TDRS,  33,   3, 13, 11, False),
    'vase':          (TDRS,  81,  66, 14, 13, False),
    'crate':         (TDRS,  97, 116, 14, 11, True),
    'folders':       (TDRS,  66,  19, 13, 12, False),

    # --- desk plants (sc_indoor_plants_free), 16x16 each ---------------------
    'deskplant_a':   (SCP,  0,  0, 16, 16, False),
    'deskplant_b':   (SCP, 16,  0, 16, 16, False),
    'deskplant_c':   (SCP, 48,  0, 16, 16, False),
    'deskplant_d':   (SCP,  0, 16, 16, 16, False),
    'deskplant_e':   (SCP, 32, 16, 16, 16, False),
    'deskplant_f':   (SCP,  0, 32, 16, 16, False),
    'deskplant_g':   (SCP, 32, 32, 16, 16, False),
    'deskplant_h':   (SCP, 16, 48, 16, 16, False),
    'watering_can':  (SCA,  0,  0, 16, 16, False),

    # --- Office-Furniture-Pixel-Art (individual files, cropped to their
    #     opaque bbox so they anchor on the floor correctly) -----------------
    # `conf_chair_back` is a BACK view -- the occupant faces away from the
    # camera, which is what the near side of a conference table needs.
    # chair_office (authored) faces down and serves the far side.
    'conf_chair_back': (OFP + r'\Chair.png',            3, 1, 10, 15, True),
    'conf_chair_side': (OFP + r'\Chair-2.png',          4, 2,  8, 13, True),
    'whiteboard_big':  (OFP + r'\Board.png',            1, 3, 14, 10, True),
    'wall_graph':      (OFP + r'\Wall-Graph.png',       2, 1, 12, 14, True),
    'wall_note':       (OFP + r'\Wall-Note.png',        2, 1, 13, 14, True),
    'bookshelf_tall':  (OFP + r'\Tall-Bookshelf.png',  10, 3, 12, 29, True),
    'plant_small':     (OFP + r'\Small-Plant.png',      1, 0, 13, 16, False),
    'plant_big':       (OFP + r'\Big-Plant.png',        7, 7, 18, 25, False),
    'boss_chair':      (OFP + r'\Boss-Chair.png',       8, 6, 16, 26, True),
}

# Five agents from one source character: suit and hair are both recoloured, so
# they read as five people rather than one man in five jackets.
AGENT_TINTS = {
    'boss_dave': (188,  92,  62),   # deep red — matches the reference's boss
    'alice':     (196, 132,  58),   # amber
    'bob':       ( 92, 118, 156),   # slate blue
    'charlie':   ( 96, 140,  96),   # green
    'eve':       (152, 104, 156),   # violet
}

AGENT_HAIR = {
    'boss_dave': (110,  74,  48),   # brown
    'alice':     (198, 150,  86),   # blonde (unchanged-ish)
    'bob':       ( 58,  46,  44),   # near-black
    'charlie':   (142,  74,  46),   # auburn
    'eve':       ( 84,  60,  52),   # dark brown
}

# ---------------------------------------------------------------------------
# Modern Interiors (purchased, full version)
#
# The header above rejected LimeZu for props because it is soft-shaded and the
# atlas was anchored on Pixel Life's hard outlines. The reference the office is
# now built against IS LimeZu's style, so the anchor flips: these are the
# primary props, and the Pixel Life / Little Bits pieces stay only where the
# pack has no equivalent (desks, monitors, keyboards -- Modern Interiors is a
# home pack and carries no office equipment at all).
#
# Picks are (sheet, index) into tools/_index/*.json, so they cannot drift the
# way raw pixel rectangles did. Regenerate an index with:
#     python tools/index_pack.py <sheet>
# and browse tools/_index/<sheet>.png to pick by number.
# ---------------------------------------------------------------------------
PACK_PICKS = {
    # --- greenery -------------------------------------------------------
    'mi_palm':          ('2_Living_Room_Singles', 12),
    'mi_palm_wide':     ('2_Living_Room_Singles', 13),
    'mi_bush':          ('2_Living_Room_Singles', 15),
    'mi_plant_tall':    ('2_Living_Room_Singles', 16),
    'mi_plant_urn':     ('2_Living_Room_Singles', 17),

    # --- lounge ---------------------------------------------------------
    'mi_armchair':      ('2_Living_Room_Singles', 19),
    'mi_armchair_b':    ('2_Living_Room_Singles', 22),
    'mi_console_lamp':  ('2_Living_Room_Singles', 31),
    'mi_console':       ('2_Living_Room_Singles', 32),
    'mi_cabinet':       ('2_Living_Room_Singles', 36),
    'mi_cabinet_b':     ('2_Living_Room_Singles', 38),
    'mi_cabinet_tall':  ('2_Living_Room_Singles', 40),
    'mi_tv_stand':      ('2_Living_Room_Singles', 50),
    'mi_sideboard':     ('2_Living_Room_Singles', 69),
    'mi_lamp_table':    ('2_Living_Room_Singles', 73),
    'mi_lamp_floor':    ('2_Living_Room_Singles', 86),
    'mi_clock_grand':   ('2_Living_Room_Singles', 89),
    'mi_bookcase':      ('2_Living_Room_Singles', 91),
    'mi_fireplace':     ('2_Living_Room_Singles', 110),
    'mi_chair':         ('2_Living_Room_Singles', 116),

    # --- kitchen --------------------------------------------------------
    'mi_counter':       ('12_Kitchen_Singles', 120),
    'mi_counter_items': ('12_Kitchen_Singles', 126),
    'mi_counter_jars':  ('12_Kitchen_Singles', 128),
    'mi_fridge':        ('12_Kitchen_Singles', 166),
    'mi_coffee':        ('12_Kitchen_Singles', 179),
    'mi_sink_unit':     ('12_Kitchen_Singles', 191),
    'mi_oven':          ('12_Kitchen_Singles', 320),
    'mi_dresser':       ('12_Kitchen_Singles', 308),

    # --- rugs (drawn on the floor layer) --------------------------------
    'mi_rug_grey':      ('1_Generic', 35),
    'mi_rug_red':       ('1_Generic', 33),
    'mi_rug_green':     ('1_Generic', 34),
    'mi_rug_blue':      ('1_Generic', 36),

    # --- conference -----------------------------------------------------
    'mi_conf_table':    ('13_Conference_Hall', 12),
    'mi_proj_screen':   ('13_Conference_Hall', 16),

    # --- misc -----------------------------------------------------------
    'mi_desk_printer':  ('1_Generic', 199),
    'mi_credenza':      ('1_Generic', 181),
    'mi_pot_plant':     ('1_Generic', 62),
    'mi_pot_bush':      ('1_Generic', 156),
}


# ---------------------------------------------------------------------------
# Modern Office - Revamped (purchased separately)
#
# Modern Interiors is a home pack and carries no office equipment at all -- no
# monitors, no keyboards, no office desks, no office chairs. This pack is where
# all of that comes from, by the same artist on the same 16x16 grid, so the two
# composite without any style correction.
#
# Its singles are each padded into a uniform 32x48 frame; pack_source.trim()
# crops that away, so the sizes in the comments below are post-trim.
# ---------------------------------------------------------------------------
# Room Builder wall courses, cut by coordinate because the wall sheet is one
# continuous atlas of 3-tile sets with no transparent gutters -- connected
# component segmentation fuses the whole thing into one blob.
#
# The reference's wall reads, top to bottom: dark top surface, cream plaster
# upper, a pale rail, then dark wood wainscot. These four cuts reproduce that.
WALL_CUTS = {
    'w_top':      (176, 296, 16, 16),   # wall seen from above (interior walls)
    'w_upper':    (  4, 364, 16, 16),   # cream plaster, flat (zero x-variance)
    'w_wainscot': (176, 322, 16, 16),   # rail + dark wood face
    'w_face':     (176, 328, 16, 16),   # wood face, no rail
}

# Thin interior partition, cut from Room_Builder_borders. Exterior walls stay
# the full 16px course -- a building's shell being thicker than its internal
# dividers is correct, and it reads that way.
#
# Only the vertical run is cut. The horizontal one used to come from its own
# place on the sheet (224, 16, 16, 8) and was a different wall entirely: mauve
# where this one is tan, and carrying a post joint every tile, so the
# conference room's north side did not match its own east and west sides. It is
# now a quarter turn of this same cut, which makes the two the same plank at
# the same 7px thickness by construction.
BORDER_CUTS = {
    'thin_v': (281, 32,  7, 16),   # vertical run, 7px wide
}
RB_BORDERS = ('furniture/final/moderninteriors/1_Interiors/16x16'
              '/Room_Builder_subfiles/Room_Builder_borders_16x16.png')

# Sliding door frames are generated from the glazing -- see main().
DOOR_OPEN_FRAMES = 8      # kept in step with canvas.js
RB_WALLS = ('furniture/final/moderninteriors/1_Interiors/16x16'
            '/Room_Builder_subfiles/Room_Builder_Walls_16x16.png')

OFFICE_PICKS = {
    # --- workstations ---------------------------------------------------
    'of_desk_l':      ('Office_Singles', 248),   # 32x32, L opens right
    'of_desk_l_alt':  ('Office_Singles', 258),   # 32x32, lighter finish
    'of_desk_l_flip': ('Office_Singles', 289),   # 32x32, L opens left
    'of_desk_top':    ('Office_Singles', 245),   # 16x16, extends a run
    'of_desk_top_b':  ('Office_Singles', 255),   # 16x16, alt finish

    # --- seating --------------------------------------------------------
    'of_chair':       ('Office_Singles', 100),   # 16x23 grey, faces down
    'of_chair_mesh':  ('Office_Singles', 101),
    'of_chair_low':   ('Office_Singles', 104),   # 16x21
    'of_chair_orange':('Office_Singles', 106),
    'of_chair_side':  ('Office_Singles', 102),   # side-on, faces right

    # --- screens and desk kit -------------------------------------------
    'of_monitor':     ('Office_Singles', 130),   # 15x15 dual, angled
    'of_monitor_b':   ('Office_Singles', 128),
    'of_monitor_flat':('Office_Singles', 132),   # 16x14 front-on
    'of_monitor_tall':('Office_Singles', 274),   # 16x35 on a stand
    'of_laptop':      ('Office_Singles', 135),   # 13x16
    'of_deskset':     ('Office_Singles', 226),   # 32x23 dual monitors + tower
    'of_deskset_b':   ('Office_Singles', 232),   # 28x23 with scanner
    'of_deskset_c':   ('Office_Singles', 224),   # 26x24 with lamp
    'of_tower':       ('Office_Singles', 306),
    'of_keyboard':    ('Office_Singles', 115),

    # --- print / wall ---------------------------------------------------
    'of_desk_lamp':   ('Office_Singles', 141),   # angled desk lamp
    'of_printer':     ('Office_Singles', 148),   # 15x19
    'of_printer_sm':  ('Office_Singles', 150),
    'of_wall_chart':  ('Office_Singles', 170),   # 30x23 line chart
    'of_wall_chart_b':('Office_Singles', 171),   # 30x23 pie + line
    'of_whiteboard':  ('Office_Singles', 207),   # 32x21
    'of_art_a':       ('Office_Singles', 95),
    'of_art_b':       ('Office_Singles', 96),

    # --- partitions -----------------------------------------------------
    # #199-205 are the CUBICLE walls: a dense grey mesh that renders as a solid
    # black slab once the lighting pass multiplies over it. The conference
    # glazing is #207 -- a light panel with a dark head rail -- posted with
    # #206. Keeping both: cubicles for screening desk ranks, glass for the
    # meeting room.
    'of_glass':       ('Office_Singles', 207),   # 32x21 glazed panel
    'of_glass_post':  ('Office_Singles', 208),   # 16x43 panel on a post
    'of_post':        ('Office_Singles', 206),   # 3x43
    'of_cubicle_h':   ('Office_Singles', 204),   # 31x24 mesh run
    'of_cubicle_v':   ('Office_Singles', 201),   # 13x38
    'of_cubicle_cnr': ('Office_Singles', 202),   # 28x38 corner
    'of_cubicle_door':('Office_Singles', 203),   # 13x24

    # --- lounge / break-room kit ----------------------------------------
    'of_vending':     ('Office_Singles', 174),
    'of_watercooler': ('Office_Singles', 172),
    'of_server':      ('Office_Singles', 175),
    'of_pod_chair':   ('Office_Singles', 196),
    'of_wall_art_c':  ('Office_Singles', 163),
    'of_backpack':    ('Office_Singles', 330),
    'of_deskset_pc':  ('Office_Singles', 316),
    'of_scanner':     ('Office_Singles', 322),
    'of_cubicle_tan': ('Office_Singles', 210),
    'of_cubicle_grey':('Office_Singles', 213),

    # --- greenery -------------------------------------------------------
    'of_plant_bushy': ('Office_Singles', 97),    # 16x28
    'of_plant_low':   ('Office_Singles', 98),    # 16x18
    'of_plant_spike': ('Office_Singles', 99),    # 12x26
}


MAN_CLIPS = {
    'idle_down': ('businessman1_idle_down.png', 3),
    'idle_up':   ('businessman1_idle_up.png', 3),
    'idle_left': ('businessman1_idle_left.png', 3),
    'idle_right':('businessman1_idle_right.png', 3),
    'walk_down': ('businessman1_walk_down.png', 4),
    'walk_up':   ('businessman1_walk_up.png', 4),
    'walk_left': ('businessman1_walk_left.png', 4),
    'walk_right':('businessman1_walk_right.png', 4),
}

FRAME_W, FRAME_H = 16, 32

# Head + shoulders, measured off the sprite: head occupies rows 2-16, torso
# starts at 17. 17 rows from the silhouette top gives a portrait that reads as a
# face rather than a whole standing body squeezed into a small box.
PORTRAIT_H = 17


def clamp(v):
    return max(0, min(255, int(round(v))))


def lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def warm_correct(img, strength=1.0):
    """Shift a sprite's mean (R-B) toward the reference warmth, preserving shading.

    Only the colour balance moves; per-pixel contrast is untouched, so outlines
    and highlights survive intact.
    """
    px = img.load()
    w, h = img.size
    op = [px[x, y] for y in range(h) for x in range(w) if px[x, y][3] > 40]
    if not op:
        return img
    warmth = sum(c[0] - c[2] for c in op) / len(op)
    delta = (TARGET_WARMTH - warmth) * strength
    if abs(delta) < 1:
        return img

    out = img.copy()
    o = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = o[x, y]
            if a <= 40:
                continue
            # Leave near-black outline pixels alone: tinting them breaks the
            # hard-outline look that made this pack match in the first place.
            if lum((r, g, b)) < 55:
                continue
            o[x, y] = (clamp(r + delta * 0.55), clamp(g + delta * 0.08),
                       clamp(b - delta * 0.45), a)
    return out


# The sprite is 16x32: head ~y2-14, torso ~y14-25, legs below. Sampling the whole
# frame picks the blonde hair (the single largest saturated area), so the suit is
# read from the torso band only.
TORSO_Y0, TORSO_Y1 = 14, 25


HAIR_Y0, HAIR_Y1 = 1, 11


def suit_cluster(frames, y0=TORSO_Y0, y1=TORSO_Y1):
    """Most common saturated mid-tone colour in a horizontal band."""
    counts = {}
    for f in frames:
        px = f.load()
        for y in range(y0, min(y1, f.size[1])):
            for x in range(f.size[0]):
                r, g, b, a = px[x, y]
                if a < 200:
                    continue
                if max(r, g, b) - min(r, g, b) < 30:
                    continue
                if not (40 < lum((r, g, b)) < 200):
                    continue
                counts[(r, g, b)] = counts.get((r, g, b), 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def retint_hair(img, dst):
    """Recolour the hair by hue family, not colour distance.

    The source hair spans five shades: shadow/mid at hue 36-45 sat ~0.43, and
    bright crown highlights at hue 55-58 sat 0.11-0.21. Gating on saturation
    misses the highlights and leaves white speckle on the crown, so the gate is
    on HUE (33-62), which still excludes skin -- skin sits at hue 29 and never
    crosses 33. H and S are remapped, V is kept, so shading survives.
    """
    dh, ds, dv = colorsys.rgb_to_hsv(dst[0] / 255, dst[1] / 255, dst[2] / 255)
    SRC_MID_V = 0.78                       # value of the source mid-tone
    out = img.copy()
    o = out.load()
    # Stop above the collar so shirt whites are never caught.
    for y in range(min(HAIR_Y1 + 1, out.size[1])):
        for x in range(out.size[0]):
            r, g, b, a = o[x, y]
            if a < 40:
                continue
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if not (33 <= h * 360 <= 62 and s >= 0.08):
                continue
            nv = max(0.0, min(1.0, v * (dv / SRC_MID_V)))
            nr, ng, nb = colorsys.hsv_to_rgb(dh, ds, nv)
            o[x, y] = (clamp(nr * 255), clamp(ng * 255), clamp(nb * 255), a)
    return out


def retint_suit(img, dst):
    """Recolour the suit by hue family, over the whole sprite.

    Same reasoning as retint_hair: the suit has several blue shades (jacket,
    collar, trousers) and colour-distance matching leaves the lighter ones
    behind as a blue ring at the collar on the back-facing frames. The blue
    family sits at hue 210-240; the red tie (hue ~0), white shirt (sat 0), skin
    and hair (hue 29-57) all fall outside it.
    """
    dh, ds, dv = colorsys.rgb_to_hsv(dst[0] / 255, dst[1] / 255, dst[2] / 255)
    SRC_MID_V = 0.61                       # value of (0, 47, 155)
    out = img.copy()
    o = out.load()
    for y in range(out.size[1]):
        for x in range(out.size[0]):
            r, g, b, a = o[x, y]
            if a < 40:
                continue
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if not (200 <= h * 360 <= 250 and s >= 0.25):
                continue
            nv = max(0.0, min(1.0, v * (dv / SRC_MID_V)))
            nr, ng, nb = colorsys.hsv_to_rgb(dh, ds, nv)
            o[x, y] = (clamp(nr * 255), clamp(ng * 255), clamp(nb * 255), a)
    return out


# Target size for the wall TVs. The source is a narrow 1:4.4, so a height that
# merely matched the old screen would reduce it to a 12px sliver with none of
# the panel detail left.
#
# 72 rather than 78 because of the lounge's east wall: floor to south course is
# 176px there, and the L-kitchen's stove (45) and vertical sink (58) plus the TV
# have to share it. At 78 that came to 181 and the stove would not fit above the
# screen; at 72 it is 175. The 6px comes off a panel that is 72 tall, which is
# not visible, and the boardroom TV has room to spare either way.
TV_WEST_H = 72


def wall_tv_west():
    """furniture/finaltv.png, keyed and reduced to a game-scale sprite.

    The file is a render, not a game sprite. Two things have to come off it:

      * the bloom -- a soft orange halo around the whole piece, which is why
        alpha is thresholded rather than resampled. Every other sprite in this
        atlas has hard edges and a feathered one shows a halo against the floor.
      * the brown wall it was posed against, which came along with the crop,
        including the wall tile's own very dark bottom border.

    Both wall parts are warm (R above B) while the TV is neutral grey and black
    all the way down to its outline, so one hue test catches the lit wall face
    and its shadowed border together -- testing brightness as well would miss
    that border, which is dark enough to read as black but is still brown.

    What is left is one connected blob: the TV and the mount hardware behind it.
    Taking the largest component drops the specks the hue test leaves behind,
    and filling holes closes the few interior pixels that read as warm.

    The reduction is a box filter, which is the right average for a smooth
    render on the way down to 16px-tile scale.
    """
    import numpy as np
    from scipy import ndimage

    src = Image.open(os.path.join(ROOT, TV_WEST)).convert('RGBA')
    a = np.array(src).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]

    solid = a[..., 3] >= 128
    wall = solid & (r - b > 14) & (g < r)
    mask = solid & ~wall

    lbl, n = ndimage.label(mask)
    if n:
        sizes = ndimage.sum(mask, lbl, range(1, n + 1))
        mask = ndimage.binary_fill_holes(lbl == int(np.argmax(sizes)) + 1)

    ys, xs = np.where(mask)
    box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    rgba = Image.fromarray(
        np.dstack([a[..., :3], mask * 255]).astype('uint8'), 'RGBA').crop(box)

    w = round(rgba.width * TV_WEST_H / rgba.height)
    small = np.array(rgba.resize((w, TV_WEST_H), Image.BOX))
    small[..., 3] = np.where(small[..., 3] >= 128, 255, 0)
    return Image.fromarray(small, 'RGBA')


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sources = {}
    pieces = []

    by_name = {}
    for name, entry in SPRITES.items():
        rel, x, y, w, h, warm = entry[:6]
        flip = entry[6] if len(entry) > 6 else False
        if rel not in sources:
            sources[rel] = Image.open(os.path.join(ROOT, rel)).convert('RGBA')
        crop = sources[rel].crop((x, y, x + w, y + h))
        if flip:
            crop = crop.transpose(Image.FLIP_LEFT_RIGHT)
        if warm:
            crop = warm_correct(crop)
        by_name[name] = crop
        pieces.append((name, crop))

    # Interior walls share the wainscot texture with the floor's colour family,
    # so an undarkened copy reads as more floor. Darkening separates them.
    def darken(img, k):
        out = img.copy()
        o = out.load()
        for yy in range(out.size[1]):
            for xx in range(out.size[0]):
                r, g, b, a = o[xx, yy]
                if a:
                    o[xx, yy] = (clamp(r * k), clamp(g * k), clamp(b * k), a)
        return out

    for src, dst, k in [('wall_mid', 'wall_side', 0.62),
                        ('wall_base', 'wall_side_base', 0.62)]:
        if src in by_name:
            pieces.append((dst, darken(by_name[src], k)))

    # Modern Interiors picks. Missing index files are fatal rather than silently
    # skipped -- a prop that vanishes from the atlas surfaces much later, as an
    # invisible hole in the office.
    for name, (sheet, i) in PACK_PICKS.items():
        pieces.append((name, pack_pick(sheet, i)))
    print(f'  + modern interiors: {len(PACK_PICKS)} sprites')

    # The pack's desks come in tan/cream/white/grey; the reference's are dark
    # walnut. Measured, its desk tops sit near (122,74,46) against the pack's
    # (212,180,130), so a per-channel multiply lands them on the reference
    # without touching the sprite's own shading structure.
    WOOD_TINT = {'of_desk_l', 'of_desk_l_alt', 'of_desk_l_flip',
                 'of_desk_top', 'of_desk_top_b'}
    # Barely a tint at all, and deliberately so.
    #
    # The reference's desks are darker than ITS floor. Reproducing that needs
    # the floor lightened to about (198,126,84) so a dark desk has something to
    # contrast against -- and that was rendered and compared side by side. The
    # lighter floor loses the warm dimness the whole scene depends on, and the
    # dark desks then disappear into the unlit corners instead. Light desks on
    # the dark floor keep both the contrast and the mood, so that is what this
    # is: just enough tint to sit in the palette.
    TINT = (0.94, 0.87, 0.80)

    def tint(img, k):
        out = img.copy()
        o = out.load()
        for yy in range(out.size[1]):
            for xx in range(out.size[0]):
                r, g, b, a = o[xx, yy]
                if a:
                    o[xx, yy] = (clamp(r * k[0]), clamp(g * k[1]),
                                 clamp(b * k[2]), a)
        return out

    rb = Image.open(os.path.join(ROOT, RB_WALLS)).convert('RGBA')
    for name, (x, y, w, h) in WALL_CUTS.items():
        pieces.append((name, rb.crop((x, y, x + w, y + h))))
    print(f'  + room builder walls: {len(WALL_CUTS)} tiles')

    bd = Image.open(os.path.join(ROOT, RB_BORDERS)).convert('RGBA')
    for name, (x, y, w, h) in BORDER_CUTS.items():
        pieces.append((name, bd.crop((x, y, x + w, y + h))))
    _tv = dict(pieces)['thin_v']
    _th = _tv.transpose(Image.ROTATE_90)
    pieces.append(('thin_h', _th))

    # End caps for the two north corners.
    #
    # A horizontal run is 16px wide and a vertical one only 7px, and both are
    # anchored on the centre of their tile, so the run's last tile always
    # sticks out past the column it is supposed to stop at -- 4px into the west
    # alley, 5px into the bullpen. The overhang cannot be placed away: moving
    # the sprite to sit flush would take it off the whole-tile grid the
    # collision depends on.
    #
    # So the caps stay full 16px tiles -- same anchor, same tile, still solid
    # in exactly the tile they occupy -- and simply carry no pixels where the
    # overhang would be. The wall then ends on the column's outer face.
    _cap_w = Image.new('RGBA', _th.size, (0, 0, 0, 0))
    _cap_w.paste(_th.crop((4, 0, 16, _th.height)), (4, 0))
    pieces.append(('thin_h_capw', _cap_w))

    _cap_e = Image.new('RGBA', _th.size, (0, 0, 0, 0))
    _cap_e.paste(_th.crop((0, 0, 11, _th.height)), (0, 0))
    pieces.append(('thin_h_cape', _cap_e))
    print(f'  + thin partitions:    {len(BORDER_CUTS)} cut + thin_h turned + 2 end caps')


    by_pack = {}
    for name, (sheet, i) in OFFICE_PICKS.items():
        img = pack_pick(sheet, i)
        if name in WOOD_TINT:
            img = tint(img, TINT)
        by_pack[name] = img
        pieces.append((name, img))
    print(f'  + modern office:    {len(OFFICE_PICKS)} sprites')

    # Side-on chair for the table ends. The pack only draws it facing right, so
    # the left-facing one is a mirror.
    if 'of_chair_side' in by_pack:
        pieces.append(('of_chair_side_l',
                       by_pack['of_chair_side'].transpose(Image.FLIP_LEFT_RIGHT)))
        print('  + derived:          of_chair_side_l')

    # A slimmer glazing run for the conference enclosure. The full 21px panel
    # reads as a chest-height wall in plan; keeping only the head rail and a
    # slim band of glass below it gives a partition that reads as thin, without
    # needing a different sprite family from the rest of the room.
    if 'of_glass' in by_pack:
        g = by_pack['of_glass']
        pieces.append(('of_glass_thin', g.crop((0, 0, g.width, 10))))
        print('  + derived:          of_glass_thin')

    # The pack's sliding-glass door is 32x32 with warm tan panels. Against a
    # 10px glazing run of pale blue-grey glass it matched on neither depth nor
    # material, and retinting a 32px sprite down to 10 loses the frame detail
    # that makes it read as a door at all.
    #
    # So the leaves are cut from the wall's own glazing instead: same sprite,
    # same palette, same depth, guaranteed to match. Each frame slides the two
    # halves apart into the wall on either side, the way a pocket door runs.
    # The conference walls are the wood partition, not glazing, so the leaves
    # are cut from a 2-tile run of that instead -- same rule as before: the door
    # is made of the wall it sits in, so it cannot mismatch.
    _p = dict(pieces)
    if 'thin_h' in _p:
        th = _p['thin_h']
        glass = Image.new('RGBA', (th.width * 2, th.height))
        glass.paste(th, (0, 0))
        glass.paste(th, (th.width, 0))
        gw, gh = glass.size
        half = gw // 2
        jamb = (48, 34, 26, 255)
        handle = (198, 158, 112, 255)
        for i in range(DOOR_OPEN_FRAMES):
            travel = round(i / (DOOR_OPEN_FRAMES - 1) * half)
            leaf = Image.new('RGBA', (gw, gh))

            # Each leaf is the glazing with a full frame drawn round it. Without
            # the frame a closed door is pixel-identical to the wall it sits in
            # -- correct for glass, but then nobody can see there is a door.
            def panel(src_x0, src_x1):
                pane = glass.crop((src_x0, 0, src_x1, gh)).copy()
                pd = ImageDraw.Draw(pane)
                pd.rectangle([0, 0, pane.width - 1, gh - 1], outline=jamb)
                # Pull handle, on the leading edge, a third of the way down.
                hx = pane.width - 2 if src_x0 == 0 else 1
                pd.line([(hx, gh // 3), (hx, gh - gh // 3)], fill=handle)
                return pane

            leaf.paste(panel(0, half), (-travel, 0), panel(0, half))
            leaf.paste(panel(half, gw), (half + travel, 0), panel(half, gw))
            pieces.append((f'door_slide_{i}', leaf))
        print(f'  + sliding door:       {DOOR_OPEN_FRAMES} frames from the glazing')

    if 'thin_v' in _p:
        tv = _p['thin_v']
        glass_v = Image.new('RGBA', (tv.width, tv.height * 2))
        glass_v.paste(tv, (0, 0))
        glass_v.paste(tv, (0, tv.height))
        gw, gh = glass_v.size
        half = gh // 2
        jamb = (48, 34, 26, 255)
        handle = (198, 158, 112, 255)
        for i in range(DOOR_OPEN_FRAMES):
            travel = round(i / (DOOR_OPEN_FRAMES - 1) * half)
            leaf = Image.new('RGBA', (gw, gh))

            def panel_v(src_y0, src_y1):
                pane = glass_v.crop((0, src_y0, gw, src_y1)).copy()
                pd = ImageDraw.Draw(pane)
                pd.rectangle([0, 0, gw - 1, pane.height - 1], outline=jamb)
                hx = gw - 2
                pd.line([(hx, pane.height // 3), (hx, pane.height - pane.height // 3)], fill=handle)
                return pane

            leaf.paste(panel_v(0, half), (0, -travel), panel_v(0, half))
            leaf.paste(panel_v(half, gh), (0, half + travel), panel_v(half, gh))
            pieces.append((f'door_slide_v_{i}', leaf))
        pieces.append(('door_slide_v', pieces[-DOOR_OPEN_FRAMES][1]))
        print(f'  + vertical sliding door: {DOOR_OPEN_FRAMES} frames')

    # Street entrance, in the building's own south course between the
    # conference room and the lounge.
    #
    # The two partition doors are cut from the partition, on the rule that a
    # door made of the wall it sits in cannot mismatch it. The shell is a
    # different wall -- a full 16px course drawn from w_top, not a 7px
    # partition -- so this one is cut from w_top for the same reason. A
    # door_slide leaf here would be a 7px sliver in a 16px opening.
    if 'w_top' in _p:
        wt = _p['w_top']
        run = Image.new('RGBA', (wt.width * 2, wt.height))
        run.paste(wt, (0, 0))
        run.paste(wt, (wt.width, 0))
        rw, rh = run.size
        half = rw // 2
        jamb = (48, 34, 26, 255)
        handle = (198, 158, 112, 255)
        for i in range(DOOR_OPEN_FRAMES):
            travel = round(i / (DOOR_OPEN_FRAMES - 1) * half)
            leaf = Image.new('RGBA', (rw, rh))

            def ext_panel(src_x0, src_x1):
                pane = run.crop((src_x0, 0, src_x1, rh)).copy()
                pd = ImageDraw.Draw(pane)
                pd.rectangle([0, 0, pane.width - 1, rh - 1], outline=jamb)
                hx = pane.width - 2 if src_x0 == 0 else 1
                pd.line([(hx, rh // 3), (hx, rh - rh // 3)], fill=handle)
                return pane

            leaf.paste(ext_panel(0, half), (-travel, 0), ext_panel(0, half))
            leaf.paste(ext_panel(half, rw), (half + travel, 0), ext_panel(half, rw))
            pieces.append((f'door_ext_{i}', leaf))
        print(f'  + entrance door:      {DOOR_OPEN_FRAMES} frames from the shell course')

    # A sofa seen from BEHIND, for the far arm of a seating group.
    #
    # The pack draws its sofas facing south only, and a U opening east needs one
    # facing north for its far arm. Turning the sprite over was the obvious move
    # and the wrong one: a flip shows the seat cushions and the two scatter
    # pillows, which is exactly what the back of a sofa hides, and it puts the
    # feet along the top edge.
    #
    # So the back is built from the sofa's own backrest instead. Rows 3..10 of
    # the source are clean backrest -- the outside face, no cushions -- and
    # tiling that band down over the seat span leaves the silhouette, the arms
    # and the feet from the real asset while replacing everything a viewer
    # standing behind the sofa could not see. Nothing is invented and nothing is
    # mirrored; it is the pack's own material, re-laid.
    _s = dict(pieces)
    if 'sofa_2seat' in _s:
        _src = _s['sofa_2seat']
        _back = _src.copy()
        _o, _i = _back.load(), _src.load()
        SEAT_X0, SEAT_X1 = 9, 45      # inside the arms' outer edges
        SEAT_Y0, SEAT_Y1 = 11, 27     # cushions and pillows, above the feet
        BACK_LO, BACK_HI = 3, 10      # the clean backrest band
        _span = BACK_HI - BACK_LO + 1
        for _y in range(SEAT_Y0, SEAT_Y1 + 1):
            _sy = BACK_LO + ((_y - SEAT_Y0) % _span)
            for _x in range(SEAT_X0, SEAT_X1 + 1):
                # Only repaint solid pixels, and only from solid ones -- the
                # arms are wider than the backrest, so parts of the band are
                # empty and would punch holes in them.
                if _i[_x, _y][3] == 0 or _i[_x, _sy][3] == 0:
                    continue
                _o[_x, _y] = _i[_x, _sy]
        pieces.append(('sofa_2seat_up', _back))
        print('  + derived:          sofa_2seat_up (back view)')

    # The round table on its own, and a chair facing the other way.
    #
    # round_table is two objects in one crop: a pedestal table in columns 0..15
    # and a stool in 17..30, with column 16 empty between them. Placing it whole
    # drops a stray stool wherever the table goes, so the dining set takes the
    # table half and puts real chairs round it instead.
    #
    # chair_wood2 faces east only, so the seat on the far side of the table is
    # its mirror -- the same left-right flip as of_chair_side_l, which keeps the
    # shading and the feet where they belong.
    _t = dict(pieces)
    if 'round_table' in _t:
        _rt = _t['round_table']
        pieces.append(('round_table_only', _rt.crop((0, 0, 16, _rt.height))))
        print('  + derived:          round_table_only')
    if 'chair_wood2' in _t:
        pieces.append(('chair_wood2_l',
                       _t['chair_wood2'].transpose(Image.FLIP_LEFT_RIGHT)))
        print('  + derived:          chair_wood2_l')

    # Props that exist in no source pack, drawn by tools/gen_tiles.py.
    # Run gen_tiles.py first -- this picks up whatever it produced.
    authored_dir = os.path.join(OUT_DIR, 'authored')
    if os.path.isdir(authored_dir):
        for fn in sorted(os.listdir(authored_dir)):
            if not fn.endswith('.png'):
                continue
            name = os.path.splitext(fn)[0]
            pieces.append((name, Image.open(os.path.join(authored_dir, fn)).convert('RGBA')))
            print(f'  + authored: {name}')
    else:
        print('  !! no authored props found — run tools/gen_tiles.py first')

    # The boardroom screen for the conference room's EAST wall.
    pieces.append(('tv_wall_west', wall_tv_west()))
    print(f'  + keyed:            tv_wall_west  ({TV_WEST})')

    # --- characters ---------------------------------------------------------
    man_frames = {}
    for clip, (fname, count) in MAN_CLIPS.items():
        path = os.path.join(ROOT, MAN_DIR, fname)
        if not os.path.exists(path):
            print(f'  !! missing {fname}')
            continue
        sheet = Image.open(path).convert('RGBA')
        man_frames[clip] = [sheet.crop((i * FRAME_W, 0, (i + 1) * FRAME_W, FRAME_H))
                            for i in range(count)]

    all_frames = [f for fs in man_frames.values() for f in fs]
    src_suit = suit_cluster(all_frames) if all_frames else None
    src_hair = suit_cluster(all_frames, HAIR_Y0, HAIR_Y1) if all_frames else None
    print(f'source suit colour: {src_suit}')
    print(f'source hair colour: {src_hair}')

    char_index = {}
    idle_down_img = {}
    for agent, tint in AGENT_TINTS.items():
        char_index[agent] = {}
        hair = AGENT_HAIR.get(agent)
        for clip, frames in man_frames.items():
            char_index[agent][clip] = []
            for i, f in enumerate(frames):
                out = retint_suit(f, tint)
                if hair:
                    out = retint_hair(out, hair)
                out = warm_correct(out, strength=0.5)
                pieces.append((f'char_{agent}_{clip}_{i}', out))
                char_index[agent][clip].append(f'char_{agent}_{clip}_{i}')
                if clip == 'idle_down' and i == 0:
                    idle_down_img[agent] = out

    # --- dock portraits -----------------------------------------------------
    # The 16x32 idle frame carries ~1px transparent margins at the sides and 2px
    # at the top, so using it straight as an avatar leaves a small, off-centre
    # face floating in its box. Crop to the real silhouette instead, using the
    # union across all agents so every card stays the same size.
    portrait_index = {}
    if idle_down_img:
        bounds = []
        for img in idle_down_img.values():
            p = img.load()
            w, h = img.size
            xs = [x for x in range(w) for y in range(h) if p[x, y][3] > 40]
            ys = [y for y in range(h) for x in range(w) if p[x, y][3] > 40]
            if xs:
                bounds.append((min(xs), min(ys), max(xs) + 1))
        x0 = min(b[0] for b in bounds)
        y0 = min(b[1] for b in bounds)
        x1 = max(b[2] for b in bounds)
        y1 = y0 + PORTRAIT_H
        for agent, img in idle_down_img.items():
            name = f'portrait_{agent}'
            pieces.append((name, img.crop((x0, y0, x1, y1))))
            portrait_index[agent] = name
        print(f'portraits: {x1 - x0}x{y1 - y0} cropped from ({x0},{y0})')

    # --- pack into a single atlas (shelf pack, 2px gutter) ------------------
    GUT = 2
    MAXW = 512
    pieces.sort(key=lambda p: -p[1].size[1])
    x = y = row_h = 0
    placed = {}
    for name, img in pieces:
        w, h = img.size
        if x + w + GUT > MAXW:
            x = 0
            y += row_h + GUT
            row_h = 0
        placed[name] = (x, y, w, h)
        x += w + GUT
        row_h = max(row_h, h)
    atlas_h = y + row_h + GUT

    atlas = Image.new('RGBA', (MAXW, atlas_h))
    for name, img in pieces:
        px, py, _, _ = placed[name]
        atlas.paste(img, (px, py))

    atlas_path = os.path.join(OUT_DIR, 'office_atlas.png')
    atlas.save(atlas_path)

    manifest = {
        'image': 'assets/generated/office_atlas.png',
        'size': [MAXW, atlas_h],
        'frames': {n: {'x': v[0], 'y': v[1], 'w': v[2], 'h': v[3]} for n, v in placed.items()},
        'characters': char_index,
        'portraits': portrait_index,
        'frame_size': [FRAME_W, FRAME_H],
    }
    man_path = os.path.join(OUT_DIR, 'office_atlas.json')
    with open(man_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=1)

    chars = sum(len(v) for clips in char_index.values() for v in clips.values())
    props = len(placed) - chars
    print(f'\nwrote {atlas_path}  ({MAXW}x{atlas_h})')
    print(f'      {props} props + {chars} character frames '
          f'({len(AGENT_TINTS)} agents x {len(man_frames)} clips)')
    print(f'wrote {man_path}')


if __name__ == '__main__':
    main()
