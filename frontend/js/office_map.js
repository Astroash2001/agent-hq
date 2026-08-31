/**
 * Office floor plan.
 *
 * 36x24 tiles of 16px -> 576x384, a 1.5:1 aspect matching a widescreen panel.
 *
 * Three spaces:
 *   - bullpen        (left)      open-plan desks, library wall, break-out nook
 *   - boss office    (top right) ornate desk, rug, reading corner
 *   - kitchen+lounge (bot right) galley kitchen, dining, TV lounge
 *
 * ---------------------------------------------------------------------------
 * PLACEMENT RULES  (all sprites are anchored bottom-centre)
 *
 *   A sprite at (x, y) occupies horizontally  (x+0.5)*16 +/- w/2
 *                                 vertically  (y+1)*16 - h  ..  (y+1)*16
 *   An AGENT at (x, y) has its baseline at y*16, not (y+1)*16.
 *
 *   1. FACING. Every sofa and armchair in the pack is drawn with the backrest
 *      at the top, i.e. they face DOWN the screen. Whatever they should look at
 *      (a TV, a coffee table) must therefore sit BELOW them.
 *   2. WALLS. South wall starts at y=368px, so a prop's baseline must be <= 366
 *      unless it is deliberately wall-mounted. East wall starts at x=560px.
 *      A ~3-tile-wide sprite on the west wall needs x >= 2 or it clips through.
 *   3. DEPTH. Draw order is by baseline, so "behind" means a smaller y. The boss
 *      sits behind his desk, chairs sit behind whoever occupies them.
 *
 *   dy     pixel nudge off the tile baseline (stands things on a desk)
 *   z      sort bias only, no movement (desk-top items need z > 0)
 *   layer  'floor' draws with the floor instead of being depth-sorted (rugs)
 * ---------------------------------------------------------------------------
 */

/**
 * L-shaped workstations, in three sizes.
 *
 * The L is two sprites: a back run (`desk_l` 48x31 / `desk_l_wide` 64x31) plus
 * `desk_l_wing` (18x32 return, one row lower at the right end). Splitting it is
 * what lets the agent sort correctly -- a single tall sprite would put the
 * desk's baseline below the occupant and hide them behind it.
 *
 * The agent sits at y+1.4 in the crook, facing up at the monitor. The wing
 * offset is chosen so its right edge lines up with the back run's:
 * +0.9 tiles for the 48px run, +1.4 for the 64px one.
 *
 * Every desk takes its own plant and desktop items so no two look alike.
 */
function deskStandard(tx, ty, plant, item) {
    return [
        { sprite: 'chair_office', x: tx - 0.2,  y: ty + 1.2, z: -1 },
        { sprite: 'desk_l',       x: tx,        y: ty },
        { sprite: 'desk_l_wing',  x: tx + 0.9,  y: ty + 1 },
        { sprite: 'monitor',      x: tx - 0.35, y: ty, dy: -24, z: 1 },
        { sprite: 'keyboard',     x: tx - 0.35, y: ty, dy: -17, z: 2 },
        { sprite: 'mouse',        x: tx + 0.3,  y: ty, dy: -17, z: 2 },
        { sprite: plant,          x: tx + 0.9,  y: ty + 1, dy: -18, z: 3 },
        { sprite: item,           x: tx - 1.0,  y: ty, dy: -18, z: 3 }
    ];
}

/** Two monitors side by side on the back run (centres 18px apart, so they touch). */
function deskDual(tx, ty, plant, itemA, itemB) {
    return [
        { sprite: 'chair_office', x: tx - 0.2,  y: ty + 1.2, z: -1 },
        { sprite: 'desk_l',       x: tx,        y: ty },
        { sprite: 'desk_l_wing',  x: tx + 0.9,  y: ty + 1 },
        { sprite: 'monitor',      x: tx - 0.7,  y: ty, dy: -24, z: 1 },
        { sprite: 'monitor',      x: tx + 0.45, y: ty, dy: -24, z: 1 },
        { sprite: 'keyboard',     x: tx - 0.15, y: ty, dy: -16, z: 2 },
        { sprite: 'mouse',        x: tx + 0.55, y: ty, dy: -16, z: 2 },
        { sprite: plant,          x: tx + 0.9,  y: ty + 1, dy: -18, z: 3 },
        { sprite: itemA,          x: tx - 1.15, y: ty, dy: -17, z: 3 },
        { sprite: itemB,          x: tx - 1.15, y: ty, dy: -25, z: 3 }
    ];
}

/**
 * Executive station: the 64px back run, dual monitors, and a three-drawer
 * pedestal standing beside it on the left. The pedestal shares the desk's
 * baseline so it reads as a matching shorter cabinet rather than floating.
 */
function deskExec(tx, ty, plant, itemA, itemB) {
    return [
        { sprite: 'chair_office',  x: tx - 0.2,  y: ty + 1.2, z: -1 },
        { sprite: 'desk_pedestal', x: tx - 2.6,  y: ty },
        { sprite: 'desk_l_wide',   x: tx,        y: ty },
        { sprite: 'desk_l_wing',   x: tx + 1.4,  y: ty + 1 },
        { sprite: 'monitor',       x: tx - 0.85, y: ty, dy: -24, z: 1 },
        { sprite: 'monitor',       x: tx + 0.3,  y: ty, dy: -24, z: 1 },
        { sprite: 'keyboard',      x: tx - 0.3,  y: ty, dy: -16, z: 2 },
        { sprite: 'mouse',         x: tx + 0.4,  y: ty, dy: -16, z: 2 },
        { sprite: plant,           x: tx + 1.4,  y: ty + 1, dy: -18, z: 3 },
        { sprite: itemA,          x: tx - 1.5,  y: ty, dy: -17, z: 3 },
        { sprite: itemB,          x: tx + 1.0,  y: ty, dy: -24, z: 3 }
    ];
}

/**
 * A run of wall partition tiles.
 *
 * `wallRun` lays `sprite` along a row from x0 to x1 inclusive; `wallCol` lays it
 * down a column. Written out rather than hand-listed because a conference room
 * wall is twenty-odd tiles and hand-listing them is how one ends up missing.
 */
function wallRun(x0, x1, y, sprite) {
    const out = [];
    for (let x = x0; x <= x1 + 0.001; x++) out.push({ sprite, x, y });
    return out;
}

function wallCol(x, y0, y1, sprite) {
    const out = [];
    for (let y = y0; y <= y1 + 0.001; y++) out.push({ sprite, x, y });
    return out;
}

/**
 * One workstation: a four-tile desk run carrying a full kit.
 *
 * The desk is TWO 32x32 L-desks side by side, not an L plus flat tops. The
 * `of_desk_top` pieces are surface only -- no apron, no legs -- so at a shared
 * baseline they sit 16px below the L-desk's surface and read as slabs lying on
 * the floor beside the desk. Two L-desks share the same apron and leg rows and
 * butt together cleanly.
 *
 * Vertical anatomy of a 32x32 desk at baseline `ty` (bottom = (ty+1)*16):
 *
 *     surface   (ty+1)*16-32 .. -6      <- desktop items belong here
 *     apron               -6 .. -3
 *     legs                -3 .. 0
 *
 * So a desktop item's dy runs about -16 (back of the desk) to -6 (front edge).
 * The chair sits 1.3 tiles lower so it overlaps the desk front, and a seated
 * agent then covers the chair.
 */
function workstation(tx, ty, opts) {
    const o = opts || {};
    const cx = tx + 1.5;                        // centre of the four-tile run

    const items = [
        { sprite: o.desk || 'of_desk_l',       x: tx + 0.5, y: ty },
        { sprite: o.desk2 || 'of_desk_l_flip', x: tx + 2.5, y: ty },

        { sprite: o.left  || 'of_monitor',   x: tx + 0.8, y: ty, dy: -14, z: 3 },
        { sprite: o.right || 'of_monitor_b', x: tx + 2.2, y: ty, dy: -14, z: 3 },
        { sprite: 'of_keyboard',   x: tx + 1.5, y: ty, dy: -6,  z: 4 },
        { sprite: 'mouse',         x: tx + 2.6, y: ty, dy: -7,  z: 4 },
        { sprite: o.plant || 'deskplant_c', x: tx + 3.4, y: ty, dy: -12, z: 3 },
        { sprite: o.clutter || 'papers', x: tx - 0.3, y: ty, dy: -8, z: 4 },
        { sprite: o.mug || 'mug',  x: tx + 0.1, y: ty, dy: -14, z: 3 },
        { sprite: o.chair || 'of_chair', x: cx,   y: ty + 1.3, z: 5 },
    ];
    return items;
}

/**
 * A rank of three stations across the bullpen, alternating the dressing.
 *
 * Three ranks of three gives the 3x3 grid of nine workstations. The bullpen's
 * usable floor is x 1..26; three 4-tile runs take 12 of that, so the remaining
 * 13 goes into two 4-tile aisles and a margin either side. Wider aisles than
 * the old four-column layout, and the grid reads as a grid.
 */
function deskRank(ty, variant) {
    const kits = [
        { desk: 'of_desk_l',     chair: 'of_chair',      plant: 'deskplant_c',
          clutter: 'papers',      mug: 'mug' },
        { desk: 'of_desk_l_alt', chair: 'of_chair_mesh', plant: 'deskplant_a',
          clutter: 'folders',     mug: 'pen_cup', right: 'of_monitor_flat' },
        { desk: 'of_desk_l',     chair: 'of_chair_low',  plant: 'deskplant_e',
          clutter: 'books_stack', mug: 'mug', left: 'of_monitor_b' },
        { desk: 'of_desk_l_alt', chair: 'of_chair',      plant: 'deskplant_g',
          clutter: 'papers',      mug: 'mug' },
    ];
    const out = [];
    [3.5, 11.5, 19.5].forEach((tx, i) => {
        out.push(...workstation(tx, ty, kits[(i + variant) % kits.length]));
    });
    return out;
}

const OFFICE_MAP = {
    cols: 40,
    rows: 24,
    tile: 16,
    scale: 2,

    rooms: {
        bullpen: { x: 1,  y: 3,  w: 22, h: 20 },
        boss:    { x: 28, y: 3,  w: 9,  h: 8  },
        lounge:  { x: 28, y: 13, w: 9,  h: 10 }
    },

    /* Warm light sources, in tile coords. `r` is the pool radius in tiles and
       `i` its strength (0-1). canvas.js bakes these into a light map that is
       multiplied over the finished world, which is what gives the reference its
       lit desks and dark corners rather than one flat-lit floor.

       Sampled from the reference: floor runs (134,78,41) under a lamp down to
       (56,25,10) in an unlit corner -- a 2.4x range that stays warm, because
       blue falls off far faster than red. */
    lights: [
        /* bullpen ceiling, three pools along the north wall */
        { x: 5,    y: 5,    r: 8,   i: 1.00 },
        { x: 12,   y: 5,    r: 8,   i: 1.00 },
        { x: 19,   y: 5,    r: 8,   i: 1.00 },
        /* bullpen ceiling, second and third rank over the desks */
        { x: 6,    y: 11,   r: 7.5, i: 0.85 },
        { x: 13.5, y: 11,   r: 7.5, i: 0.85 },
        { x: 20.5, y: 11,   r: 7.5, i: 0.85 },
        { x: 8,    y: 15.5, r: 6.5, i: 0.62 },
        { x: 16,   y: 15.5, r: 6.5, i: 0.62 },
        /* conference room: the wall screen and the console lamp */
        { x: 12,   y: 19.5, r: 7,   i: 0.60 },
        { x: 19.4, y: 18.4, r: 4.5, i: 0.80 },
        /* boss office */
        { x: 33,   y: 5,    r: 8,   i: 0.90 },
        { x: 29.4, y: 8.6,  r: 4.5, i: 0.85 },
        /* kitchen + lounge */
        { x: 34,   y: 14,   r: 7,   i: 0.70 },
        { x: 29.2, y: 14.4, r: 4.5, i: 0.90 },
        { x: 29, y: 20.2, r: 3.5, i: 0.70 },
        { x: 31.8, y: 21.4, r: 4,   i: 0.55 }
    ],

    /* Automatic doors: entry vertical sliding door on the west alley partition.
       The leaves are 32px tall and hang from (y+1)*16, so the door is exactly
       two tile rows. y=19 puts them at 288..320 -- the gap the wall runs leave
       at rows 18-19, and the two tiles the collision grid leaves open, so the
       only way through the wall is the only place it looks open. */
    doors: [
        { x: 3, y: 19, v: true, trigger: 3.4 },
        /* Street entrance, in the south shell course. It lands in the corridor
           between the conference room's east wall (col 23) and the divider that
           fronts the lounge (col 27), which is the one stretch of outside wall
           with open floor behind it and no room to cut into. Two tiles wide,
           matching the 32px leaves. */
        { x: 24, y: 23, kind: 'door_ext', trigger: 3.4 }
    ],

    /* Ceiling downlights: small fixtures on the north wall rail, each washing
       a glow cone down the wall below it -- the thing that makes the lights
       read as actual lights instead of a vignette. x is the fixture centre;
       they sit on the wall course (y is fixed by the renderer). */
    fixtures: [
        { x: 4.5 }, { x: 9.5 }, { x: 14.5 }, { x: 19.5 },   /* bullpen  */
        { x: 30.5 }, { x: 33.5 }, { x: 36.5 }               /* boss     */
    ],

    /* The three floating blocks the reference draws, in tile units, measured to
       the OUTSIDE of each room's wall ring. The 1-tile divider at x=24 and the
       one at y=11 fall between blocks and so render as voids, which is what
       separates the rooms visually. See canvas.js buildShell(). */
    blocks: [
        /* One building. The rooms are divided by real walls, not by voids, so
           the shell is a single rounded rectangle -- see canvas.js buildShell. */
        { x: 0, y: 0, w: 40, h: 24 }
    ],

    walls: [
        { x: 0,  y: 0,  len: 40 },
        { x: 0,  y: 3,  len: 20, v: true },
        { x: 39, y: 3,  len: 20, v: true },
        { x: 0,  y: 23, len: 40 },
        { x: 27, y: 3,  len: 20, v: true },
        { x: 28, y: 11, len: 12 }
    ],

    doorways: [
        { x: 27, y: 9,  h: 2 },
        { x: 27, y: 17, h: 2 },
        /* No opening in the row-11 wall. The boss office and the kitchen side
           are separate rooms now, each reached from the corridor at col 27 by
           its own door -- rows 9-10 and 17-18. Taking it out also frees the
           floor beneath it, which is what lets the dining set move up. */
        /* The entrance opening. Punching it drops these two tiles from the
           solid grid, which is both what lets anyone stand in the doorway and
           what stops drawWalls painting the shell course across the leaves. */
        { x: 24, y: 23, w: 2 }
    ],

    props: [
        /* ==================== BULLPEN ====================
           Rebuilt on the Modern Office pack. Three ranks of workstations with
           a planter run between each, then the glazed conference room across
           the south end -- the arrangement in the reference.

           Row pitch is 4 tiles: desk at y, chair at y+1.3, planters at y+2.6.
           Column pitch is 5 tiles, which clears the 32px desk plus its plant. */

        /* --- north wall -------------------------------------------------
           Two courses, and each one sits where its kind belongs.

           The wall itself is py 0..47; the floor starts at 48. The furniture is
           TOP-aligned on 48, so every piece's back edge lands on the wall's own
           bottom row -- touching it, never crossing into it. It used to share a
           baseline at py 88 instead, which left the shallow pieces floating up
           to 20px out in the room while the bookshelf, being deepest, was the
           only thing reaching the wall and pushed 6px through it.

           The art moved the other way, up onto the wall course itself, bottoms
           on py 44. It is wall-mounted, so that is where it should have been;
           at py 45..67 it straddled the join, three pixels into the wall and
           the rest hanging on the floor.

           Separating them pays for itself twice: art and furniture can no
           longer overlap, so the pictures no longer have to be squeezed into
           the gaps above the low units and are simply spaced 34px apart along
           the whole wall.

           The one ordering constraint is the bookshelf. At 47px it is the only
           piece deeper than the 41px between the wall and the desks, so it sits
           at px 127..172 -- inside the gap between the first and second
           workstations, where its depth reaches py 94 with nothing under it. */

        { sprite: 'of_art_a',         x: 1.28,   y: 1.78 },
        { sprite: 'of_wall_chart',    x: 4.78,   y: 1.78 },
        { sprite: 'wall_clock',       x: 8.31,   y: 1.78 },
        { sprite: 'of_whiteboard',    x: 11.91,  y: 1.78 },
        { sprite: 'pic_frame_a',      x: 15.41,  y: 1.78 },
        { sprite: 'of_wall_chart_b',  x: 18.84,  y: 1.78 },
        { sprite: 'wall_note',        x: 22.31,  y: 1.78 },
        { sprite: 'of_art_b',         x: 25.28,  y: 1.78 },

        { sprite: 'mi_cabinet',       x: 1.62,   y: 4.47 },
        { sprite: 'water_cooler',     x: 3.53,   y: 3.22 },
        { sprite: 'coffee_machine',   x: 5.03,   y: 3.03 },
        { sprite: 'bookshelf_tall',   x: 6.47,   y: 3.84 },
        { sprite: 'bookshelf',        x: 8.91,   y: 4.97 },
        { sprite: 'mi_counter',       x: 11.88,  y: 2.91 },
        { sprite: 'mi_cabinet_b',     x: 14.25,  y: 4.03 },
        { sprite: 'filing_cab',       x: 16.22,  y: 3.16 },
        { sprite: 'of_plant_bushy',   x: 17.84,  y: 3.78 },
        { sprite: 'mi_sideboard',     x: 19.81,  y: 3.47 },
        { sprite: 'of_server',        x: 22.0,   y: 4.16 },
        { sprite: 'of_printer',       x: 23.81,  y: 3.22 },
        { sprite: 'of_printer_sm',    x: 25.38,  y: 3.16 },

        /* --- west wall ---------------------------------------------------
           The strip between the building wall (ends px 15) and the first desk
           column (starts px 56) is only 40px wide, which is why the 46px
           bookshelf that used to stand here had its spine buried in the
           masonry. It has moved into the north run, where it fits; the west
           wall now carries a column of pieces narrow enough for the space,
           every one of them left-aligned on px 20 and evenly spaced down to
           the conference partition. of_scanner and mi_plant_tall are new --
           both were sitting unused in the atlas. */
        { sprite: 'of_scanner',       x: 1.59,   y: 6.78 },
        { sprite: 'mi_bookcase',      x: 1.16,   y: 9.28 },
        { sprite: 'of_plant_low',     x: 1.28,   y: 11.53 },
        { sprite: 'mi_plant_tall',    x: 1.22,   y: 14.47 },

        /* --- east side, against the lounge divider ------------------------
           Right edges all on px 435, the divider's own pixels starting at 436.
           bookshelf_wide used to run to 439 and had its last four columns
           inside the wall. */
        { sprite: 'of_plant_bushy',   x: 26.28,  y: 6.8 },
        { sprite: 'cabinet_tall',     x: 26.38,  y: 13.1 },
        /* Coat stand, tucked into the corner the entrance makes with the
           lounge divider: px 422..435 across and 324..367 down. The divider's
           own pixels start at 436 and the south course at 368, so it stands
           hard in the corner with nothing crossing either -- and it clears the
           doorway itself, which ends at 415, so it never fouls the leaves. */
        { sprite: 'coat_rack',        x: 26.32,  y: 22.0 },

        /* --- two ranks of three workstations in the bullpen -------------
           No partitions and no planter runs between the ranks: the desks stand
           in open rows, which is how they read best at this scale. */
        ...deskRank(8.1, 0),

        ...deskRank(12.5, 1),

        /* ==================== EXPANDED CONFERENCE ROOM ====================
           Spacious boardroom enclosed with standard partition borders.
           Boardroom screen hung on the right (East) side wall facing the table. */

        /* North partition. Every partition below sits on WHOLE tile
           coordinates, and that is the point of them.

           A prop is drawn from its own x/y but blocked in the tile that
           Math.floor of that x/y lands in, so a partition placed off-grid is
           solid somewhere other than where it is painted. On the old numbers
           the east wall was drawn in column 24 and blocked column 23, and the
           north wall was drawn in row 15 and blocked row 14 -- which is why
           agents walked with a wall through them instead of round it. On whole
           tiles each 7px plank sits inside the very tile it blocks.

           thin_v fills its tile row top to bottom, so the vertical runs meet
           end to end and meet the door exactly; thin_h fills its tile column
           left to right, so the north run meets both corners. */
        { sprite: 'thin_h_capw', x: 3,  y: 15 },
        ...wallRun(4, 22, 15, 'thin_h'),
        { sprite: 'thin_h_cape', x: 23, y: 15 },

        /* West wall, broken by the sliding door onto the west alley. Rows
           16..22, less rows 18-19 for the door: upper 256..288, door 288..320,
           lower 320..368, meeting the building's south course at 368. The
           runs start at row 16 rather than 15 so their tops land on the north
           partition's underside -- at 14.8 they used to stand 8px proud of it,
           which is the spur that showed at both north corners. */
        ...wallCol(3, 16, 17, 'thin_v'),
        ...wallCol(3, 20, 22, 'thin_v'),

        /* East wall, the same rows unbroken. */
        ...wallCol(23, 16, 22, 'thin_v'),

        /* Boardroom TV on the right (East) side wall, screen facing west across
           the table. x seats its back against the partition's inner face (map
           px 372); y centres its 78px run on the table's centre line rather than
           on the table's anchor row, so it sits opposite the middle seats. */
        { sprite: 'tv_wall_west',    x: 22.2,   y: 20.9,  z: 3 },

        /* Architectural whiteboard, hung on the north partition. Its top sits on
           the partition's own top edge (py 249) so it reads as fixed to the
           wall; the north seats draw over its lower edge, which is what you
           would see looking down on a board behind a row of chairs. */
        { sprite: 'whiteboard_arch', x: 7.5,    y: 16.94, z: 2 },

        /* The whole table group sits half a tile below the room's own centre
           line, not on it. The rug is 72px in a 122px interior (north partition
           at py 252, side walls ending at 374), so 20.8 leaves 24px of floor
           above it and 26px below -- near enough balanced, where the old 20.3
           left 16 above and 34 below and read as pushed up against the wall. */
        { sprite: 'conference_rug',  x: 13.5,   y: 20.8, layer: 'floor' },

        /* Seven a side, plus one drawn up at each end of the table. */
        { sprite: 'of_chair',        x: 8.0,    y: 17.6 },
        { sprite: 'of_chair',        x: 9.8,    y: 17.6 },
        { sprite: 'of_chair',        x: 11.6,   y: 17.6 },
        { sprite: 'of_chair',        x: 13.5,   y: 17.6 },
        { sprite: 'of_chair',        x: 15.4,   y: 17.6 },
        { sprite: 'of_chair',        x: 17.2,   y: 17.6 },
        { sprite: 'of_chair',        x: 19.0,   y: 17.6 },

        { sprite: 'of_chair_side',   x: 6.6,    y: 19.8, z: 2 },
        { sprite: 'of_chair_side_l', x: 20.4,   y: 19.8, z: 2 },

        { sprite: 'board_table',     x: 13.5,   y: 19.8 },
        { sprite: 'of_laptop',       x: 8.5,    y: 19.8, dy: -16, z: 3 },
        { sprite: 'deskplant_a',     x: 11.0,   y: 19.8, dy: -16, z: 3 },
        { sprite: 'deskplant_b',     x: 13.5,   y: 19.8, dy: -16, z: 3 },
        { sprite: 'deskplant_a',     x: 16.0,   y: 19.8, dy: -16, z: 3 },
        { sprite: 'tablet',          x: 18.0,   y: 19.8, dy: -16, z: 3 },
        { sprite: 'papers',          x: 19.4,   y: 19.8, dy: -16, z: 3 },

        { sprite: 'of_chair_low',    x: 8.8,    y: 20.8, z: 4 },
        { sprite: 'of_chair_low',    x: 10.7,   y: 20.8, z: 4 },
        { sprite: 'of_chair_low',    x: 12.6,   y: 20.8, z: 4 },
        { sprite: 'of_chair_low',    x: 14.4,   y: 20.8, z: 4 },
        { sprite: 'of_chair_low',    x: 16.3,   y: 20.8, z: 4 },
        { sprite: 'of_chair_low',    x: 18.2,   y: 20.8, z: 4 },

        /* Potted greenery in the two corners of the entry wall, flanking the
           door: the spike above it, the bushy one below.

           Both are seated on x so their left edge lands on px 59. The west
           partition's own pixels stop at 58, so they stand hard against its
           inner face without a single pixel crossing into the alley -- which
           is what they used to do, the east one straddling its column and both
           of them growing up through the north band. Vertically they clear the
           door's 288..319 run: the spike finishes at 281, the bushy one starts
           at 340 and stops on the building's south course at 367. */
        { sprite: 'of_plant_spike',  x: 3.6,    y: 16.63 },
        { sprite: 'of_plant_bushy',  x: 3.7,    y: 22.0  },

        /* Corridor/alley outside the room's west wall: clock removed, corner pot kept. */
        { sprite: 'of_plant_low',    x: 1.5,    y: 21.4 },

        /* ==================== BOSS OFFICE ==================== */
        { sprite: 'rug_green',    x: 32, y: 8, layer: 'floor' },

        /* Boss sits BEHIND the desk: his baseline (6.5*16=104) is above the
           desk's ((6.6+1)*16=121.6), so the desk draws over his lower half. */
        /* A working desk, not a writing table. desk_ornate was 20x23 with one
           monitor perched on it; this is the same four-tile L-run the bullpen
           uses, so the boss's desk reads as part of the same office. Centred on
           the rug -- desk run px 488..551, rug 472..567, both on 519.5. */
        ...workstation(30.5, 7.0, { chair: 'boss_chair', clutter: 'folders',
                                    plant: 'deskplant_b' }),

        { sprite: 'bookshelf',    x: 29,   y: 4.8 },
        { sprite: 'cabinet_wide', x: 36,   y: 4.8 },
        { sprite: 'pic_frame_a',  x: 33,   y: 3.1 },
        { sprite: 'wall_clock',   x: 35,   y: 3.1 },

        /* Reading corner: armchair faces down, side table beside it */
        { sprite: 'armchair_down', x: 35.5, y: 9.6 },
        { sprite: 'side_table',   x: 34,   y: 9.6 },
        { sprite: 'teapot',       x: 34,   y: 9.6, dy: -20, z: 2 },
        /* Moved out of the doorway. At (28.4, 9.8) it stood on px 454..468 by
           126..171 -- squarely on tiles (28,9) and (28,10), which are the floor
           just inside the boss office's own door, and with furniture solid that
           sealed the room off entirely. It now stands by the armchair. */
        { sprite: 'floor_lamp',   x: 38.0,  y: 9.97 },
        { sprite: 'plant',        x: 28.4, y: 6.6 },
        { sprite: 'of_plant_bushy', x: 36.9, y: 8.4 },
        { sprite: 'mi_bookcase',    x: 31.6, y: 3.9 },
        { sprite: 'of_art_b',       x: 34.6, y: 3.1 },

        /* ==================== KITCHEN + LOUNGE ====================
           Rebuilt from the Modern Interiors pack against the new reference.

           Block is x 25-36, y 12-24; the east wall sits at x=35, so the usable
           floor is x 26-34 by y 13-23.

           Sprite heights drive the north run: a 32x48 fridge placed at tile y
           has its top edge at (y+1)*16-48, so y=14 is the first row where it
           clears the block's own top edge at px 192. The 32x32 counters share
           that baseline so the whole run lines up. */

        /* Kitchen, in an L along the north and east walls.
           Everything sits right of the entry, which now lands at px 448..479 --
           nothing is drawn in that landing down to py 255, so the way down from
           the boss office is clear.

           The horizontal arm is top-aligned on py 192, the first floor row, and
           its units butt together across px 503..606. The vertical arm picks up
           at px 607 -- the very next pixel -- and runs down the east wall, so
           the two meet at the corner rather than leaving a seam. counter_sink_v
           is the pack's only vertical counter, which is what makes the turn
           possible at all.

           The vertical arm stops at py 249 because the lounge TV starts at 281;
           there is no room below it for a second unit. */
        /* NORTH arm: fridge then the cabinet run, both top-aligned on py 192
           and butted together across px 520..593. */
        { sprite: 'fridge',           x: 32.84, y: 14.47 },
        { sprite: 'counter_run',      x: 35.16, y: 13.78 },
        { sprite: 'teapot',           x: 34.44, y: 13.78, dy: -31, z: 2 },
        { sprite: 'vase',             x: 35.72, y: 13.78, dy: -31, z: 2 },

        /* EAST arm: stove at the corner, sink running down below it, both
           flush to the wall at px 623. The cabinet run ends at px 593 and the
           stove starts at 594, so the two arms meet at the corner; down the
           wall the stove ends py 236 and the sink starts 237. */
        { sprite: 'oven',             x: 37.59, y: 13.84 },
        { sprite: 'counter_sink_v',   x: 38.03, y: 17.47 },
        /* Dining area, set in the floor the entry opens onto. Three seats round
           a pedestal table, every one of them turned to face it: chair_wood
           looks south from the head, chair_wood2 east from the west side, and
           its mirror west from the east side. The south side is left open
           because that is the way in from the room.

           round_table_only is the table half of round_table -- the pack fused a
           stool to its right in the same crop, and placing the whole thing
           dropped a stray stool beside every table it was used for.

           The set starts 38px below the doorway, so there is more than two
           tiles of clear floor between the opening and the first chair back.
           The lamp stands off to the east at px 502..516, clear of both the
           table and the fridge that starts at 520. */
        { sprite: 'chair_wood',       x: 28.75, y: 13.28 },
        { sprite: 'chair_wood2',      x: 27.69, y: 15.28 },
        { sprite: 'round_table_only', x: 28.78, y: 15.09 },
        { sprite: 'mug_tdr',          x: 28.78, y: 15.09, dy: -13, z: 2 },
        { sprite: 'chair_wood2_l',    x: 29.94, y: 15.28 },
        { sprite: 'floor_lamp',       x: 31.38, y: 14.66 },

        /* ==================== ENTERTAINMENT & LOUNGE ====================
           The same suite as before -- sofa across the top, armchair on the left,
           sofa across the bottom, table in the middle of a rug, screen on the
           right -- but assembled from the packs instead of one baked 133x110
           picture of a room. That image carried its own floor and shadows, so
           it sat as a rectangle of somebody else's lighting on top of ours, and
           being a single sprite nothing in it could be moved, walked round or
           reused. It also overhung the south course by 12px.

           The screen is tv_wall_west, the same asset as the boardroom's, hung
           on the east wall with its face to the west so the seating looks at
           it. Its back is on px 623, the wall's inner face.

           Group box: px 485..623 by 272..367. */
        /* Three sofas on three sides of the table, the fourth side left open
           to the screen -- a U facing east.

           Everything in it shares one centre line, py 319.5: the table, the
           back sofa and the TV. Across, the table's centre (532.5) sits on the
           arms' centre (533). The last attempt had none of that -- the table
           had ended up at px 575..598, outside the seating entirely, wedged
           between a sofa and the TV, with the north sofa 40px west of it.

           The arms are the pack's 49x31 sofas and the back is sofa_side, the
           only east-facing one at 24x50 -- the largest seats the packs hold.
           sofa_2seat_up is the south arm: the packs ship nothing facing north,
           so it is their own sofa turned over, the same mirror trick as
           of_chair_side_l. Its feet read at the top edge, which is the price.

           Group px 485..557 by 272..367, leaving 48px of viewing distance from
           the arms to the screen. */
        { sprite: 'rug_green',        x: 32.84, y: 21.03, layer: 'floor' },
        { sprite: 'sofa_3seat',       x: 32.88, y: 17.97 },
        /* Follows the TV down: both centres are on py 330.5, so the sofa still
           faces the screen square on. */
        { sprite: 'sofa_side',        x: 30.59, y: 21.28 },
        /* 21.97, not 22.03: at 22.03 its baseline fell on py 368, which is the
           south course's own first row -- the sofa was standing on the wall
           rather than in front of it. This puts its feet on py 366. */
        { sprite: 'sofa_2seat_up',    x: 32.88, y: 21.97 },
        /* dining_table, not coffee_table: 46x30 against the old 24x19, which
           left the middle of the U looking empty. It is the only clean bigger
           table the packs hold -- round_table is a fused crop with a stool
           attached to its right, and bench_table is a bench. At px 510..555 by
           304..333 it sits 1-2px clear of both arms and the back sofa.

           The three pieces on it hang off the table's own y with dy:-14, which
           drops their baselines on py 320 -- the bottom edge of the tabletop
           rather than the floor -- so they read as standing on it. Same trick
           as the boardroom's laptop and planters. */
        { sprite: 'dining_table',     x: 32.84, y: 19.91 },
        { sprite: 'books_stack',      x: 31.84, y: 19.91, dy: -14, z: 2 },
        { sprite: 'vase',             x: 32.84, y: 19.91, dy: -14, z: 2 },
        { sprite: 'mug_tdr',          x: 33.88, y: 19.91, dy: -14, z: 2 },
        /* Dropped to py 295..366 so the kitchen's east arm has room above it.
           Floor to south course on that wall is 176px, and stove 45 + sink 58 +
           TV must share it -- at the old 78 tall that came to 181 and the stove
           had nowhere to go, so the panel is 72 now. It picks up one pixel
           below the sink. */
        { sprite: 'tv_wall_west',     x: 38.03, y: 21.97, z: 3 },

        /* Room accents and greenery */
        { sprite: 'of_plant_bushy',       x: 28.6, y: 21.8 },
    ],

    seats: {
        /* y 6.0, not 6.5: at 6.5 the seat point sat inside desk_ornate's own
           footprint, so with furniture solid the boss could never stand on it. */
        orchestrator: { x: 32.0, y: 8.3,  facing: 'up',   room: 'boss'    },
        /* x = desk column - 0.2, matching the chair, so the agent sits in the
           crook of the L. y = desk row + 1.4 keeps their baseline below the
           main run's, so they draw in front of it. */
        researcher:   { x: 5,    y: 9.4,  facing: 'up',   room: 'bullpen' },
        coder:        { x: 13,   y: 9.4,  facing: 'up',   room: 'bullpen' },
        data_analyst: { x: 21,   y: 9.4,  facing: 'up',   room: 'bullpen' },
        creative:     { x: 5,    y: 13.8, facing: 'up',   room: 'bullpen' }
    },

    hangouts: [
        { x: 28.8, y: 18.8, label: 'water cooler'  },
        { x: 28.8, y: 19.4, label: 'dining table'  },
        /* In front of the suite, not on it: at (31, 21.4) this sat inside the
           back sofa, which is walk-through no longer. */
        { x: 36.25, y: 20.0, label: 'lounge'        },
        { x: 31.5, y: 16.2, label: 'kitchen'       },
        { x: 5.2,  y: 5.2,  label: 'whiteboard'    },
        { x: 2.6,  y: 12.4, label: 'library'       },
        /* Both of these used to sit inside the table they are named after --
           fine while furniture was walk-through, unreachable now. They stand
           beside it instead. */
        { x: 11.0, y: 21.0, label: 'conference room' }
    ],

    /* Plate text before the backend answers. Without these the plaques read
       'researcher' / 'data_analyst' on a cold load, which is not what the
       reference shows. The manifests overwrite them once /agents returns. */
    agentNames: {
        orchestrator: 'Boss Dave',
        researcher:   'Alice',
        data_analyst: 'Bob',
        coder:        'Charlie',
        creative:     'Eve'
    },

    agentSprites: {
        orchestrator: 'boss_dave',
        researcher:   'alice',
        data_analyst: 'bob',
        coder:        'charlie',
        creative:     'eve'
    }
};

if (typeof module !== 'undefined' && module.exports) module.exports = { OFFICE_MAP };
