/**
 * AI Agent HQ — tilemap office renderer.
 *
 * Replaces the previous procedural renderer, which drew every desk and chair as
 * ctx.fillRect calls. Pixel art cannot be produced that way; this draws real
 * sprites out of the generated atlas (see tools/build_atlas.py).
 *
 * Draw order:
 *   0  floor      — synthesised 16x48 plank blocks, variant-scattered
 *   1  walls      — peach upper course over brown wainscot
 *   2  depth list — props AND agents in one list, sorted each frame by baseline
 *                   (y + height), so an agent can walk behind a desk
 *   3  overlay    — speech bubbles, task orbs
 */

const ATLAS_URL = 'assets/generated/office_atlas.png';
const ATLAS_MANIFEST = 'assets/generated/office_atlas.json';
const FLOOR_URL = 'assets/generated/floor_wood.png';

const FLOOR_VARIANTS = 4;
const FLOOR_BLOCK_H = 48;

/* ---------------------------------------------------------------------------
   Callout palette, sampled from final.jpg (see css/style.css for the method).
   The plaque is near-black with a warm grey rule; the balloon is the flat
   off-white the reference uses, not pure #fff.
   --------------------------------------------------------------------------- */
/* Lighting, solved rather than eyeballed.

   The reference's floor runs (56,25,10) unlit to (134,78,41) lit. Our floor
   paints at (161,91,62), so the multipliers those two ends need are
   (0.35,0.27,0.16) and (0.83,0.86,0.66) -- i.e. a light map running from
   rgb(89,70,41) to rgb(212,219,168).

   AMBIENT is that dark end. LIGHT_* is the DIFFERENCE a full-strength pool adds
   on top of it, which is why green leads red here: the ambient is already
   red-heavy, so reaching a neutral-warm peak takes proportionally more green
   and blue, not more red. */
const AMBIENT = 'rgb(122, 100, 70)';
const LIGHT_R = 90;
const LIGHT_G = 119;
const LIGHT_B = 98;

/* Downlight glow: the additive wash a wall fixture throws below itself, and
   the warm point of the fixture face. Additive, so it reads as emitted light
   on top of the multiplied shadow rather than as un-shadowed floor. */
const GLOW_COLOR   = [255, 196, 110];
const FIXTURE_LIT  = '#ffd898';
const FIXTURE_BODY = '#241a16';

/* Room shell: the orange rim over a dark edge that outlines each block. Widths
   are world pixels, i.e. before the zoom, so the rim keeps its proportion to
   the tiles at any window size -- which is how the reference reads. */
const SHELL_RADIUS     = 6;
const SHELL_RIM        = 3;
const SHELL_EDGE_W     = 1;
const SHELL_RIM_COLOR  = '#cd7142';
const SHELL_EDGE       = '#4a221f';

/* Frame bands, outermost to innermost. Sampled off the reference's border. */
const FRAME_OUTER      = '#17100e';
const FRAME_MID        = '#4a2b1c';
const FRAME_LIT        = '#a35f34';
const FRAME_INNER      = '#5e3520';

/* Wall edges. Lit on the north and east faces, shaded on south and west, with
   a short cast shadow onto the floor below -- enough to give a one-tile wall
   some height without modelling it. */
const WALL_EDGE_LIT    = 'rgba(214, 168, 118, 0.55)';
const WALL_EDGE_SHADE  = 'rgba(28, 16, 12, 0.45)';
const WALL_CAST        = 'rgba(20, 11, 8, 0.30)';

/* Sliding door: the sheet holds a full open-then-close cycle, but only
   the opening half is used -- the door's position is eased both ways, so
   playing the closing frames as well would double the travel. */
const DOOR_OPEN_FRAMES = 8;

const LABEL_FONT  = "'Geist', ui-sans-serif, system-ui, 'Segoe UI', sans-serif";
const PLATE_BG    = '#120d13';
const PLATE_EDGE  = '#8a6a58';
const PLATE_NAME  = '#e8a94a';
const PLATE_VERB  = '#ded9d2';
/* Warm paper rather than flat white, so a balloon belongs to a lamplit room. */
const BUBBLE_BG   = '#f7efe2';
const BUBBLE_EDGE = '#2a1d14';
const BUBBLE_FG   = '#1f150d';

/* Status tags: the small pills over a head that say what an agent is doing
   when it is not saying anything. Same ink as the HUD, so the floor and the
   sidebar read as one interface. */
const TAG_BG      = 'rgba(17, 13, 11, 0.9)';
const TAG_EDGE    = 'rgba(255, 226, 190, 0.18)';
const TAG_FG      = '#f2e9dd';
const TAG_MUTED   = '#bfb1a1';
const TAG_OK      = '#7fd48c';
const TAG_ACCENT  = '#f0b35e';
const TAG_BAD     = '#ff7a66';
// How long a finished or failed tag stays up before the agent goes quiet.
const DONE_TAG_MS = 4000;

const WALK_SPEED = 2.4;          // tiles per second
const FRAME_MS = 140;            // per walk frame

// Two agents standing closer than this overlap on screen: the figures are 18px
// wide on a 16px tile, so anything under about 1.15 tiles reads as one person.
const MIN_SEP = 1.15;
// Fan-out radii, in tiles, tried in order around a destination already taken,
// and how many directions are sampled on each.
const SPREAD_RINGS = [1.25, 1.9, 2.6, 3.3];
const SPREAD_DIRS = 12;
// Tighter radii, for finding the floor beside a point that is inside furniture.
const SNAP_RINGS = [0.8, 1.2, 1.6, 2.1, 2.6];
// How close a destination must be to a station before it counts as that
// station, and so gets its seats rather than the generic fan-out.
const SEAT_RING_REACH = 2.5;
// Shortest walk worth announcing. Under this the figure barely moves, so a
// bubble naming somewhere it is going reads as a lie.
const MIN_TRIP = 2.0;

class PixelOfficeCanvas {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        this.ctx.imageSmoothingEnabled = false;

        this.map = OFFICE_MAP;
        this.tile = this.map.tile;
        this.scale = this.map.scale;
        this.baseWidth = this.map.cols * this.tile;
        this.baseHeight = this.map.rows * this.tile;

        this.isPaused = false;
        this.simSpeed = 1;
        this.selectedAgentId = null;
        // Latches once the backend sends its first agent.move.
        this.liveDriven = false;
        this.particles = [];

        this.atlas = null;
        this.frames = {};
        this.characters = {};
        this.floorImg = null;
        this.ready = false;

        // Doors ease open/closed rather than snapping, so `open` is a 0..1
        // position rather than a boolean.
        this.doors = (this.map.doors || []).map(d => ({ ...d, open: 0 }));

        // Two grids, because the same data cannot serve both jobs. `solid` is
        // the masonry the renderer paints as wall tiles. `blocked` is what an
        // agent may not walk through, which is the masonry PLUS the conference
        // room's partitions -- those are drawn as props, so painting them from
        // a grid too would double them up.
        this.propRects = [];
        this.solid = this.buildCollision();
        this.blocked = this.buildCollision(true);
        this.agents = this.buildAgents();

        this.lastTime = performance.now();
        this.init();
    }

    /* ===================== setup ===================== */

    init() {
        this.resize();
        window.addEventListener('resize', () => this.resize());
        this.canvas.addEventListener('click', (e) => this.handleClick(e));
        this.load();
        requestAnimationFrame((t) => this.loop(t));
    }

    load() {
        // Cache-bust all three together. The browser caches the atlas PNG and
        // its manifest independently, so after a rebuild it can pair a fresh
        // PNG with a stale JSON -- every frame then reads from the wrong
        // rectangle and the office renders as scrambled furniture. One shared
        // token per load keeps the pair consistent.
        const v = `?v=${Date.now()}`;
        const atlas = new Image();
        atlas.src = ATLAS_URL + v;
        const floor = new Image();
        floor.src = FLOOR_URL + v;

        Promise.all([
            new Promise(res => { atlas.onload = res; atlas.onerror = res; }),
            new Promise(res => { floor.onload = res; floor.onerror = res; }),
            fetch(ATLAS_MANIFEST + v).then(r => r.json()).catch(() => null)
        ]).then(([, , manifest]) => {
            this.atlas = atlas.naturalWidth ? atlas : null;
            this.floorImg = floor.naturalWidth ? floor : null;
            if (manifest) {
                this.frames = manifest.frames || {};
                this.characters = manifest.characters || {};
                // Footprints need sprite sizes, which only exist once the
                // manifest lands, so both the rect list and the grid that
                // depends on it are built here rather than in the constructor.
                this.propRects = this.buildPropRects();
                this.blocked = this.buildCollision(true);
            }
            this.ready = !!(this.atlas && manifest);
            if (window.retroHUD) {
                window.retroHUD.addTerminalLog(
                    this.ready
                        ? `Office atlas loaded — ${Object.keys(this.frames).length} sprites.`
                        : 'Atlas missing — run: python tools/build_atlas.py',
                    this.ready ? 'SUCCESS' : 'ERROR');
            }
        });
    }

    /**
     * Furniture footprints, in world pixels.
     *
     * Seen from above a prop's sprite IS its floor area, so the whole rect is
     * what an agent has to walk round. Three kinds are left out: rugs and
     * anything with a `dy`, which stands on another prop rather than the floor;
     * seating, because agents are meant to end up on it; and wall art, which
     * hangs on the wall course and is not in the room at all.
     */
    buildPropRects() {
        const T = this.tile;
        // Only small chairs stay walk-through, and only because they are drawn
        // up to tables: an agent has to be able to reach the one at its desk,
        // and the conference and dining seats ring their tables so tightly that
        // blocking them would wall those rooms off.
        //
        // Sofas, armchairs and benches DO block. They are big enough to walk
        // behind, and a prop with a lower baseline paints over whoever is
        // standing there -- which is what "walking under the furniture" was.
        const SEATING = ['chair'];
        const WALL_ART = ['wall_chart', 'whiteboard', 'wall_clock', 'wall_note',
                          'wall_graph', 'pic_frame', 'of_art', 'wall_art',
                          'extinguisher', 'chart_board'];
        const out = [];
        for (const p of this.map.props) {
            const f = this.frames[p.sprite];
            if (!f) continue;
            if (p.layer === 'floor' || p.dy !== undefined) continue;
            if (p.sprite.startsWith('thin_')) continue;
            if (SEATING.some(k => p.sprite.includes(k))) continue;
            if (WALL_ART.some(k => p.sprite.includes(k))) continue;
            const l = Math.round((p.x + 0.5) * T - f.w / 2);
            const b = Math.round((p.y + 1) * T);
            out.push({ l, r: l + f.w, t: b - f.h, b });
        }
        return out;
    }

    /**
     * Is this point inside a piece of furniture? Tile coordinates in, since
     * that is what an agent's position is.
     *
     * An agent is always allowed onto its own seat: seats are drawn up to
     * desks, so the point one sits on is inside the desk's own footprint by
     * design, and without this nobody could reach their chair.
     */
    blockedByProp(tx, ty, agent) {
        if (agent && agent.seat &&
            Math.abs(tx - agent.seat.x) < 0.7 && Math.abs(ty - agent.seat.y) < 0.7) {
            return false;
        }
        const px = tx * this.tile, py = ty * this.tile;
        for (const r of this.propRects) {
            if (px >= r.l && px < r.r && py >= r.t && py < r.b) return true;
        }
        return false;
    }

    /**
     * Wall runs -> a solid grid, with doorways punched back out.
     *
     * With `withPartitions`, the conference room's thin_v/thin_h props are
     * marked too. They are the only room walls in the map built out of props
     * rather than `walls` entries, so nothing was stopping an agent walking
     * straight through them -- the sliding door was decoration, opening only
     * because someone happened to pass near the sensor, never because anyone
     * had to go through it. The runs leave x=3, y=17..18 unmarked on their own,
     * which is exactly the gap the door fills, so the doorway needs no special
     * case here.
     */
    buildCollision(withPartitions = false) {
        const { cols, rows, walls, doorways } = this.map;
        const grid = Array.from({ length: rows }, () => new Uint8Array(cols));

        const mark = (x, y, v) => {
            if (x >= 0 && x < cols && y >= 0 && y < rows) grid[y][x] = v;
        };

        for (const w of walls) {
            for (let i = 0; i < w.len; i++) {
                if (w.v) mark(w.x, w.y + i, 1);
                else mark(w.x + i, w.y, 1);
            }
        }
        // The north wall is a 3-tile-tall course.
        for (let x = 0; x < cols; x++) { mark(x, 1, 1); mark(x, 2, 1); }

        if (withPartitions) {
            for (const p of this.map.props) {
                // thin_v, thin_h and the two north-corner end caps.
                if (p.sprite.startsWith('thin_')) {
                    mark(Math.floor(p.x), Math.floor(p.y), 1);
                }
            }
            // Furniture, so routes go round it instead of straight into it. A
            // tile counts as furniture only when its CENTRE is covered:
            // marking every tile a sprite so much as touches walls the office
            // in -- measured, and it left exactly one square walkable.
            for (let ty = 0; ty < rows; ty++) {
                for (let tx = 0; tx < cols; tx++) {
                    const px = tx * this.tile + this.tile / 2;
                    const py = ty * this.tile + this.tile / 2;
                    for (const r of this.propRects) {
                        if (px >= r.l && px < r.r && py >= r.t && py < r.b) {
                            mark(tx, ty, 1);
                            break;
                        }
                    }
                }
            }
            // A desk covers the tile its own chair stands on, so seats are
            // punched back out or nobody could be routed to their own desk.
            for (const seat of Object.values(this.map.seats || {})) {
                mark(Math.floor(seat.x), Math.floor(seat.y), 0);
            }
        }

        for (const d of doorways) {
            for (let dy = 0; dy < (d.h || 1); dy++) {
                for (let dx = 0; dx < (d.w || 1); dx++) mark(d.x + dx, d.y + dy, 0);
            }
        }
        return grid;
    }

    isSolid(tx, ty) {
        const x = Math.floor(tx), y = Math.floor(ty);
        if (x < 0 || y < 0 || x >= this.map.cols || y >= this.map.rows) return true;
        return this.blocked[y][x] === 1;
    }

    /**
     * Masonry at this tile? `isSolid` answers "may an agent stand here", which
     * is masonry PLUS furniture; this answers "is there a wall here", which is
     * the only question the wall renderer should be asking.
     */
    isWall(tx, ty) {
        if (tx < 0 || ty < 0 || tx >= this.map.cols || ty >= this.map.rows) return true;
        return this.solid[ty][tx] === 1;
    }

    /** The building shell: the outer course, three tiles deep along the north. */
    isPerimeter(tx, ty) {
        return tx === 0 || tx === this.map.cols - 1 ||
               ty <= 2 || ty === this.map.rows - 1;
    }

    /**
     * Which way does the partition on this tile run? Horizontal when a wall
     * continues east or west of it and nothing walls it in above or below.
     *
     * Masonry only, deliberately. Asking `isSolid` here meant the fridge and
     * counters standing along row 12 counted as "walled in below", which
     * flipped seven tiles of the boss office / kitchen divider from lengths of
     * wall to free-standing posts -- the divider simply stopped halfway along
     * and carried on as a row of stubs with floor showing between them.
     */
    isHorizontalRun(tx, ty) {
        return (this.isWall(tx - 1, ty) || this.isWall(tx + 1, ty)) &&
               !(this.isWall(tx, ty - 1) || this.isWall(tx, ty + 1));
    }

    buildAgents() {
        return Object.entries(this.map.seats).map(([id, seat]) => ({
            id,
            name: this.map.agentNames[id] || id,
            sprite: this.map.agentSprites[id],
            x: seat.x,
            y: seat.y,
            seat: { x: seat.x, y: seat.y, facing: seat.facing },
            // Where this agent has undertaken to end up. Seeded with its own
            // desk so the very first claim already knows the desks are taken.
            goal: { x: seat.x, y: seat.y, facing: seat.facing },
            goalFor: { x: seat.x, y: seat.y },
            room: seat.room,
            facing: seat.facing,
            state: 'idle',
            path: null,
            waypoints: [],
            walkTimer: 0,
            walkFrame: 0,
            idleTimer: 2000 + Math.random() * 6000,
            errand: null,
            speech: null
        }));
    }

    resize() {
        const rect = this.canvas.parentElement.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
        this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
        this.viewW = rect.width;
        this.viewH = rect.height;
        this.dpr = dpr;

        // Integer-only zoom looks marginally crisper but throws away up to half
        // the available scale (a 2.41 fit renders at 2x and leaves a wide black
        // border), so we scale continuously.
        //
        // Contain, not cover: the shell rim is drawn OUTSIDE the map bounds and
        // the reference floats the blocks on black with a margin all round, so
        // cropping the edges would shear the frame off the top and left.
        const pad = SHELL_RIM + SHELL_EDGE_W * 2 + 4;
        this.zoom = Math.min(rect.width  / (this.baseWidth  + pad * 2),
                             rect.height / (this.baseHeight + pad * 2));
        this.offsetX = Math.floor((rect.width - this.baseWidth * this.zoom) / 2);
        this.offsetY = Math.floor((rect.height - this.baseHeight * this.zoom) / 2);
        this.ctx.imageSmoothingEnabled = false;
    }

    /* ===================== public API (used by app.js / hud.js) ===================== */

    setAgents(manifests) {
        manifests.forEach(m => {
            const a = this.agents.find(x => x.id === m.id);
            if (!a) return;
            a.name = m.name || a.name;
        });
    }

    /**
     * Put a specialist hired mid-mission on the floor, at the street door.
     *
     * There are five characters in the atlas and the packs that produced them
     * are gigabytes and not in this repository, so a contractor has no art of
     * its own. It borrows one, hue-rotated by `sprite_hue` so two hires never
     * look alike, and `drawAgent` pins a visitor badge on it -- which is the
     * part that matters. An agent the office hired for twenty minutes must not
     * be indistinguishable from permanent staff, or the whole mechanism is
     * invisible in the one view that exists to show it.
     */
    addAgent(manifest, seat, entrance) {
        if (!manifest || !manifest.id) return null;
        if (this.agents.find(x => x.id === manifest.id)) return null;

        const door = entrance || { x: 24.5, y: 22.4 };
        const desk = seat || door;
        const agent = {
            id: manifest.id,
            name: manifest.name || manifest.id,
            sprite: manifest.sprite || 'bob',
            hue: manifest.sprite_hue || 0,
            spawned: true,
            x: door.x,
            y: door.y,
            seat: { x: desk.x, y: desk.y, facing: 'up' },
            goal: { x: desk.x, y: desk.y, facing: 'up' },
            goalFor: { x: desk.x, y: desk.y },
            room: 'bullpen',
            facing: 'up',
            state: 'idle',
            path: null,
            waypoints: [],
            walkTimer: 0,
            walkFrame: 0,
            idleTimer: 2000 + Math.random() * 6000,
        };
        this.agents.push(agent);
        // Walk in rather than appear. The backend is already blocking for the
        // travel time it computed, so this is the same arrival, drawn.
        this.sendTo(agent, agent.seat, false);
        return agent;
    }

    /** End every contract: the contractors leave, the permanent staff stay. */
    removeSpawnedAgents() {
        this.agents = this.agents.filter(a => !a.spawned);
    }

    /**
     * A hue-rotated copy of one character's frames, built once and cached.
     *
     * Done on an offscreen canvas rather than with a ctx filter on every draw:
     * `filter` is re-parsed per call and this runs for every frame of every
     * walking agent, and it would tint the shadow and the name plate with it.
     */
    tintedAtlas(hue) {
        if (!hue || !this.atlas) return this.atlas;
        this._tinted = this._tinted || {};
        if (this._tinted[hue]) return this._tinted[hue];

        const off = document.createElement('canvas');
        off.width = this.atlas.width;
        off.height = this.atlas.height;
        const c = off.getContext('2d');
        c.filter = `hue-rotate(${hue}deg) saturate(1.15)`;
        c.drawImage(this.atlas, 0, 0);
        this._tinted[hue] = off;
        return off;
    }

    updateAgentStatus(agentId, status) {
        const a = this.agents.find(x => x.id === agentId);
        if (!a) return;
        a.state = status;
        a.stateAt = Date.now();
        if (status === 'active' || status === 'tool_calling') {
            this.sendTo(a, a.seat, false);    // back to its own desk to work
        } else if (status === 'done') {
            // No bubble here: the only thing that pops one is work arriving.
            a.tool = null;
            a.queued = null;
            this.spawnCelebration(a.x, a.y);
        } else if (status === 'error') {
            // A failure used to look exactly like standing still. It is now an
            // event: a red burst where it happened and a tag that stays up.
            a.tool = null;
            a.queued = null;
            this.spawnCelebration(a.x, a.y, ['#ff7a66', '#ff9e8c', '#c8503c', '#2a1d14']);
        }
    }

    /** The tool an agent is running right now, or null when it returns. */
    setAgentTool(agentId, tool) {
        const a = this.agents.find(x => x.id === agentId);
        if (a) a.tool = tool ? { name: String(tool), at: Date.now() } : null;
    }

    /**
     * Waiting at a busy station, from station.queued: {label, occupied_by,
     * queue_length}. Null clears it on station.acquired.
     */
    setAgentQueued(agentId, info) {
        const a = this.agents.find(x => x.id === agentId);
        if (!a) return;
        a.queued = info ? {
            label: info.label || info.station_id || 'station',
            by: info.occupied_by || '',
            length: info.queue_length || 0,
        } : null;
    }

    showSpeech(agentId, text) {
        const a = this.agents.find(x => x.id === agentId);
        // Sized for a task description rather than a one-word status: three
        // wrapped lines is what drawBubble will show, and it needs long enough
        // on screen to actually be read.
        if (a && text) a.speech = { text: String(text).slice(0, 96), expiry: Date.now() + 6500 };
    }

    /**
     * Walk an agent somewhere because the BACKEND said so.
     *
     * office.py already decides that, say, the researcher must stand at the
     * library before web_search will run, and sends the trip as agent.move.
     * Until this existed the canvas ignored that and ran its own random
     * wander, so the figures on screen had nothing to do with the work being
     * done. The first backend-driven move latches `liveDriven`, which retires
     * the wander for good -- the two would otherwise fight over the same
     * waypoints.
     */
    walkAgentTo(agentId, x, y, label) {
        const a = this.agents.find(v => v.id === agentId);
        if (!a || typeof x !== 'number' || typeof y !== 'number') return;
        this.liveDriven = true;
        this.sendTo(a, { x, y });
        // A tag, not a balloon: balloons are kept for what an agent SAYS --
        // its task, its argument -- so a walk does not drown them out.
        if (label) this.setHeading(a, label.replace(/^to the /, ''));
    }

    setHeading(a, label) {
        a.heading = { label, until: Date.now() + 2500 };
    }

    /**
     * Backend says the agent has arrived. Deliberately does NOT move anyone.
     *
     * office.py's walk duration is a timing model, not an animation: it billed
     * the library trip at 250ms, while the renderer covers that same distance
     * in about 1.2s at WALK_SPEED -- and further still once the path bends
     * round furniture. So arrival lands mid-stride nearly every time, and
     * setting the position here snapped the figure to the destination. That is
     * what looked like teleporting.
     *
     * The destination is already right, because walkAgentTo set it; the walk
     * just needs to finish. Only when nothing is in flight -- a reload, or a
     * move whose path could not be solved -- is this a real desync worth
     * correcting.
     */
    agentArrived(agentId, x, y) {
        const a = this.agents.find(v => v.id === agentId);
        if (!a) return;
        if (a.path || (a.waypoints && a.waypoints.length)) return;
        if (typeof x !== 'number' || typeof y !== 'number') return;

        // Snapping straight onto the backend's coordinate is what stacked a
        // meeting: it sends every attendee the table's one point, so four
        // agents placed here all land on the same tile. If this is the trip
        // they already claimed a seat for, that seat IS the arrival; a scrub or
        // a reload that never walked claims one now.
        const same = a.goalFor &&
                     Math.hypot(a.goalFor.x - x, a.goalFor.y - y) < 0.6;
        const spot = same && a.goal ? a.goal : this.claimSpot(a, { x, y });
        a.goal = spot;
        a.goalFor = { x, y };
        a.x = spot.x;
        a.y = spot.y;
        if (spot.facing) a.facing = spot.facing;
    }

    /**
     * Work handed from one agent to another.
     *
     * This used to be a 2.5px dot arcing between two desks, which read as a
     * network ping rather than anything an office does. Now the sender walks
     * over, and the bubble app.js pops carries what the task actually is; the
     * walk is an ordinary sendTo, so a later backend move simply overrides it.
     */
    handOffTask(sourceId, targetId) {
        const from = this.agents.find(a => a.id === sourceId) || this.agents[0];
        const to = this.agents.find(a => a.id === targetId);
        if (!from || !to || from === to) return;
        // Stand beside the desk, not on it -- one tile south of the seat.
        this.sendTo(from, { x: to.seat.x, y: to.seat.y + 1.1 });
        from.errand = { returnTo: { ...from.seat }, at: Date.now() + 4200 };
    }

    focusAgent(agentId) {
        this.selectedAgentId = agentId;
        const a = this.agents.find(x => x.id === agentId);
        if (a) this.showSpeech(agentId, `${a.name} reporting.`);
    }

    spawnCelebration(tx, ty, palette = ['#f8c873', '#e08a3c', '#67d94e', '#ece7de']) {
        for (let i = 0; i < 14; i++) {
            this.particles.push({
                x: tx, y: ty - 1.2,
                vx: (Math.random() - 0.5) * 0.09,
                vy: (-Math.random() * 0.09) - 0.02,
                life: 1,
                c: palette[i % palette.length]
            });
        }
    }

    /* ===================== movement ===================== */

    /**
     * BFS over the tile grid. Straight-line movement gets stuck on the wall
     * between the bullpen and the right-hand rooms -- agents have to be routed
     * through the doorways, so we need real pathfinding rather than a heading.
     */
    findPath(sx, sy, tx, ty) {
        const { cols, rows } = this.map;
        const start = [Math.floor(sx), Math.floor(sy)];
        const goal = [Math.floor(tx), Math.floor(ty)];
        if (start[0] === goal[0] && start[1] === goal[1]) return [];

        const key = (x, y) => y * cols + x;
        const prev = new Int32Array(cols * rows).fill(-1);
        const seen = new Uint8Array(cols * rows);
        const q = [start];
        seen[key(...start)] = 1;
        let found = false;

        while (q.length) {
            const [cx, cy] = q.shift();
            if (cx === goal[0] && cy === goal[1]) { found = true; break; }
            for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
                const nx = cx + dx, ny = cy + dy;
                if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
                const k = key(nx, ny);
                if (seen[k] || this.blocked[ny][nx]) continue;
                seen[k] = 1;
                prev[k] = key(cx, cy);
                q.push([nx, ny]);
            }
        }
        if (!found) return null;

        const out = [];
        let k = key(...goal);
        while (k !== key(...start) && k >= 0) {
            out.push({ x: (k % cols) + 0.5, y: Math.floor(k / cols) + 0.9 });
            k = prev[k];
        }
        out.reverse();
        return out;
    }

    /**
     * The seat list for the place `dest` belongs to, or null for open floor.
     *
     * Matched by proximity to a station rather than by name, because a
     * destination arrives as a bare coordinate -- from the backend's
     * agent.move, from a hangout, from a click -- and never as an id.
     */
    seatRing(dest) {
        const rings = this.map.gatherSpots || {};
        const stations = this.map.stations || {};
        for (const [id, spots] of Object.entries(rings)) {
            const at = stations[id];
            if (!at) continue;
            if (Math.hypot(at.x - dest.x, at.y - dest.y) <= SEAT_RING_REACH) return spots;
        }
        return null;
    }

    /**
     * Is another agent standing on this spot, or walking to it?
     *
     * Claims have to be read off destinations, not positions: four agents sent
     * to a meeting are all still mid-stride when the last of them picks a seat,
     * so comparing where everyone currently IS would hand out the same chair
     * four times. `goal` is where each agent has undertaken to end up.
     */
    spotTaken(spot, self, sep) {
        return this.agents.some((a) => {
            if (a === self) return false;
            const at = a.goal || a;
            return Math.hypot(at.x - spot.x, at.y - spot.y) < sep;
        });
    }

    /** Floor an agent can actually stand on -- not masonry, not furniture. */
    isStandable(x, y, agent) {
        return !this.isSolid(x, y) && !this.blockedByProp(x, y, agent);
    }

    /**
     * The closest floor you can stand on, searched outward from `point`.
     *
     * The mirror of office.py's `_nearest_open`, and needed for the same
     * reason it gives: "a station sits on the furniture it IS", so a station's
     * own coordinate is routinely a blocked tile. The backend has always
     * snapped; the renderer walked at the raw point instead, which only worked
     * because no station happened to sit on its furniture yet.
     */
    nearestOpen(point, agent) {
        if (this.isStandable(point.x, point.y, agent)) return { ...point };
        for (const r of SNAP_RINGS) {
            for (let i = 0; i < SPREAD_DIRS; i++) {
                const th = (i / SPREAD_DIRS) * Math.PI * 2;
                const x = point.x + Math.cos(th) * r;
                const y = point.y + Math.sin(th) * r;
                if (this.isStandable(x, y, agent)) {
                    return { x, y, facing: point.facing || null };
                }
            }
        }
        return { ...point };
    }

    /**
     * Can you get from one point to the other without crossing anything?
     *
     * Straight-line, sampled every quarter tile. The fan-out below needs it
     * because "not a wall tile" is not the same question as "the same place":
     * the dining table sits a tile and a half from the corridor partition, so
     * an offset that cleared the furniture check still put an agent on the far
     * side of that wall, in the bullpen, under a bubble reading "heading to the
     * dining table". Rejecting those costs nothing -- there are two dozen
     * candidates and the destination itself as the fallback.
     */
    walkLineClear(from, to) {
        const steps = Math.ceil(Math.hypot(to.x - from.x, to.y - from.y) / 0.25);
        for (let i = 1; i <= steps; i++) {
            const t = i / steps;
            if (this.isSolid(from.x + (to.x - from.x) * t,
                             from.y + (to.y - from.y) * t)) return false;
        }
        return true;
    }

    /**
     * Where `agent` actually stands when it is sent to `dest`.
     *
     * The office sends several agents to one point routinely -- every attendee
     * of a meeting gets the conference table's single coordinate, and any two
     * idlers can pick the same hangout -- and they used to stack on it exactly,
     * so a table of four looked like one person standing alone.
     *
     * A place with chairs drawn round it hands out those chairs. Everywhere
     * else the first arrival gets the spot it asked for and later ones fan out
     * around it, skipping anything solid, furnished, or already claimed.
     */
    claimSpot(agent, dest) {
        const ring = this.seatRing(dest);
        if (ring) {
            // Seats are far enough apart that identity, not distance, is the
            // question: is anyone else sitting in this exact chair?
            const free = ring.find(s => !this.spotTaken(s, agent, 0.5));
            if (free) return { ...free };
        }
        // The floor at the place, which is not always the place: a station's
        // point can be inside the shelving it names.
        const at = this.nearestOpen(dest, agent);
        if (!this.spotTaken(at, agent, MIN_SEP)) return at;

        // Every offset that is real floor, in the same room, and not inside
        // furniture. Collected rather than returned on the first hit, because
        // a cramped place -- the library alcove holds four before it runs out
        // -- needs a fallback better than standing on somebody.
        const open = [];
        for (let ring = 0; ring < SPREAD_RINGS.length; ring++) {
            const r = SPREAD_RINGS[ring];
            for (let i = 0; i < SPREAD_DIRS; i++) {
                // Half-step alternate rings so the outer spots fall in the gaps
                // of the inner ones instead of directly behind them.
                const th = (i + (ring % 2 ? 0.5 : 0)) / SPREAD_DIRS * Math.PI * 2;
                const spot = {
                    x: at.x + Math.cos(th) * r,
                    y: at.y + Math.sin(th) * r,
                    facing: dest.facing || null
                };
                if (!this.isStandable(spot.x, spot.y, agent)) continue;
                if (!this.walkLineClear(at, spot)) continue;
                if (!this.spotTaken(spot, agent, MIN_SEP)) return spot;
                open.push(spot);
            }
        }
        // Cramped: take the emptiest patch of floor there is rather than stack.
        let best = null, bestGap = -1;
        for (const spot of open) {
            let gap = Infinity;
            for (const other of this.agents) {
                if (other === agent) continue;
                const at = other.goal || other;
                gap = Math.min(gap, Math.hypot(at.x - spot.x, at.y - spot.y));
            }
            if (gap > bestGap) { bestGap = gap; best = spot; }
        }
        return best || at;
    }

    /**
     * `claim` is off only for a trip to the agent's OWN seat, which is its
     * alone by construction -- claiming there could only ever push somebody off
     * their own desk because a visitor happened to be standing beside it.
     */
    sendTo(agent, dest, claim = true) {
        const goal = claim ? this.claimSpot(agent, dest) : { ...dest };
        agent.goal = goal;
        agent.goalFor = { x: dest.x, y: dest.y };
        const route = this.findPath(agent.x, agent.y, goal.x, goal.y);
        // Unreachable (or already there): go direct rather than freezing.
        agent.waypoints = route && route.length ? route.slice(0, -1) : [];
        agent.waypoints.push({ x: goal.x, y: goal.y, facing: goal.facing || null });
        agent.path = agent.waypoints.shift();
    }

    stepAgent(a, dt) {
        if (a.path) {
            const dx = a.path.x - a.x;
            const dy = a.path.y - a.y;
            const dist = Math.hypot(dx, dy);
            const step = WALK_SPEED * dt * this.simSpeed;

            if (dist <= step) {
                a.x = a.path.x;
                a.y = a.path.y;
                if (a.path.facing) a.facing = a.path.facing;
                // Advance to the next waypoint, or finish the route.
                a.path = (a.waypoints && a.waypoints.length) ? a.waypoints.shift() : null;
                if (!a.path) {
                    a.walkFrame = 0;
                    a.idleTimer = 4000 + Math.random() * 9000;
                }
            } else {
                const nx = a.x + (dx / dist) * step;
                const ny = a.y + (dy / dist) * step;
                // Slide along walls and furniture rather than sticking to
                // them -- and rather than walking through. Each axis is tested
                // on its own so a blocked diagonal still slides.
                if (!this.isSolid(nx, a.y) && !this.blockedByProp(nx, a.y, a)) a.x = nx;
                if (!this.isSolid(a.x, ny) && !this.blockedByProp(a.x, ny, a)) a.y = ny;

                a.facing = Math.abs(dx) > Math.abs(dy)
                    ? (dx > 0 ? 'right' : 'left')
                    : (dy > 0 ? 'down' : 'up');

                a.walkTimer += dt * 1000 * this.simSpeed;
                if (a.walkTimer >= FRAME_MS) {
                    a.walkTimer = 0;
                    a.walkFrame = (a.walkFrame + 1) % 4;
                }
            }
            return;
        }

        // An errand: whoever delivered a task walks back to their own desk.
        if (a.errand && Date.now() >= a.errand.at) {
            this.sendTo(a, a.errand.returnTo, false);
            a.errand = null;
            return;
        }

        // Idle wander -- but only while nothing real is driving the floor. Once
        // the backend has moved anybody, the office shows the work instead of
        // inventing motion.
        if (this.liveDriven) return;

        a.idleTimer -= dt * 1000 * this.simSpeed;
        if (a.idleTimer <= 0) {
            if (a.state === 'active' || a.state === 'thinking') {
                a.idleTimer = 6000;
                return;
            }
            // Somewhere the agent is NOT already standing. Picking blind meant
            // announcing "heading to the water cooler" over a figure that then
            // did not move, because the hangout it drew was the one under its
            // own feet -- findPath returns an empty route when start and goal
            // share a tile, so the bubble was the only thing that happened.
            // office.py guards its own walks the same way (walk_to returns
            // early inside 0.25 tiles); this is the renderer's half of it.
            const spots = this.map.hangouts.filter(
                s => Math.hypot(s.x - a.x, s.y - a.y) >= MIN_TRIP);
            const goHome = Math.random() < 0.5 || !spots.length;
            if (goHome) {
                this.sendTo(a, a.seat, false);
            } else {
                const spot = spots[Math.floor(Math.random() * spots.length)];
                this.sendTo(a, spot);
                this.setHeading(a, spot.label);
            }
            a.idleTimer = 5000 + Math.random() * 8000;
        }
    }

    /* ===================== loop ===================== */

    loop(now) {
        const dt = Math.min(0.05, (now - this.lastTime) / 1000);
        this.lastTime = now;

        if (!this.isPaused) this.update(dt);
        this.draw();
        requestAnimationFrame((t) => this.loop(t));
    }

    /** Automatic doors: open while any agent is within `trigger` tiles. */
    stepDoors(dt) {
        for (const d of this.doors) {
            // Sensor centre has to be the centre of the leaves the draw code
            // actually places. A horizontal door is anchored at (x+1)*T so its
            // centre is d.x + 1, but a vertical one is anchored at (x+0.5)*T --
            // measuring that one as d.x + 1 too put the sensor half a tile
            // inside the wall, so the door opened and shut off-centre.
            const cx = d.v ? d.x + 0.5 : d.x + (d.w || 2) / 2;
            const near = this.agents.some(a =>
                Math.hypot(a.x - cx, a.y - d.y) < (d.trigger || 3.2));
            const target = near ? 1 : 0;
            // Opens faster than it closes, the way a real sensor door behaves.
            const rate = target > d.open ? 5.5 : 2.2;
            const step = rate * dt * this.simSpeed;
            d.open += Math.max(-step, Math.min(step, target - d.open));
            d.open = Math.max(0, Math.min(1, d.open));
        }
    }

    update(dt) {
        const now = Date.now();
        this.stepDoors(dt);
        for (const a of this.agents) {
            if (a.speech && now > a.speech.expiry) a.speech = null;
            this.stepAgent(a, dt);
        }
        for (let i = this.particles.length - 1; i >= 0; i--) {
            const p = this.particles[i];
            p.x += p.vx * this.simSpeed;
            p.y += p.vy * this.simSpeed;
            p.life -= dt * 1.5 * this.simSpeed;
            if (p.life <= 0) this.particles.splice(i, 1);
        }
    }

    /* ===================== drawing ===================== */

    drawSprite(name, px, py, anchorBottomCentre = true, source = null) {
        const f = this.frames[name];
        if (!f || !this.atlas) return;
        const w = f.w, h = f.h;
        const dx = anchorBottomCentre ? px - w / 2 : px;
        const dy = anchorBottomCentre ? py - h : py;
        const img = source || this.atlas;
        this.ctx.drawImage(img, f.x, f.y, w, h, Math.round(dx), Math.round(dy), w, h);
    }

    draw() {
        const ctx = this.ctx;
        ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
        // Cleared, not filled: the stage behind the canvas is a CSS gradient,
        // and the office should float on it rather than on a black slab.
        ctx.clearRect(0, 0, this.viewW, this.viewH);

        if (!this.ready) {
            ctx.fillStyle = '#85786b';
            ctx.font = `13px ${LABEL_FONT}`;
            ctx.textAlign = 'center';
            ctx.fillText('Building office…  run  python tools/build_atlas.py',
                         this.viewW / 2, this.viewH / 2);
            return;
        }

        ctx.save();
        ctx.translate(this.offsetX, this.offsetY);
        ctx.scale(this.zoom, this.zoom);

        if (!this.shell) this.shell = this.buildShell();

        // A soft contact shadow under the whole building, so it sits on the
        // stage instead of being pasted onto it. shadowBlur is in screen
        // pixels whatever the transform, hence the zoom factor.
        ctx.save();
        ctx.shadowColor = 'rgba(0, 0, 0, 0.6)';
        ctx.shadowBlur = 28;
        ctx.shadowOffsetY = 10;
        ctx.fillStyle = '#120c09';
        ctx.fill(this.shell);
        ctx.restore();

        this.drawShellRim(ctx, this.shell);

        ctx.save();
        ctx.clip(this.shell);
        this.drawFloor(ctx);
        this.drawWalls(ctx);
        this.drawDepthSorted(ctx);
        this.drawOrbs(ctx);
        this.applyLighting(ctx);
        ctx.restore();

        ctx.restore();

        // Labels are screen-space: see drawOverlay.
        this.drawOverlay(ctx);
    }

    drawFloor(ctx) {
        const T = this.tile;
        if (!this.floorImg) {
            ctx.fillStyle = '#a1613e';
            ctx.fillRect(0, 0, this.baseWidth, this.baseHeight);
            return;
        }
        for (let ty = 0; ty < this.map.rows; ty++) {
            for (let tx = 0; tx < this.map.cols; tx++) {
                // Deterministic variant scatter: same layout every load, but not
                // periodic, so the floor never reads as wallpaper.
                const v = ((tx * 73856093) ^ (ty * 19349663)) % FLOOR_VARIANTS;
                const sv = (v + FLOOR_VARIANTS) % FLOOR_VARIANTS;
                const sy = (ty * T) % FLOOR_BLOCK_H;
                ctx.drawImage(this.floorImg, sv * T, sy, T, T, tx * T, ty * T, T, T);
            }
        }

        // Rugs are an overlay on the floor, not depth-sorted props: sorting one
        // by its baseline paints it over the desk and the agent standing on it.
        for (const p of this.map.props) {
            if (p.layer !== 'floor') continue;
            this.drawSprite(p.sprite, (p.x + 0.5) * T, (p.y + 1) * T + (p.dy || 0));
        }
    }

    drawWalls(ctx) {
        const T = this.tile;
        for (let ty = 0; ty < this.map.rows; ty++) {
            for (let tx = 0; tx < this.map.cols; tx++) {
                if (!this.solid[ty][tx]) continue;
                // North course reads top -> mid -> base. Everything else is an
                // interior/side wall, drawn from the darkened variant so it
                // separates from the floor instead of blending into it.
                // North course reads as a real wall elevation: dark top
                // surface, cream plaster, then the rail and wood wainscot the
                // reference uses. Every other wall is seen purely from above,
                // so it gets the top surface only.
                // Exterior shell keeps the full 16px course; interior
                // dividers are thin partitions, which is both architecturally
                // right and what stops the office reading as a bunker.
                const perimeter = this.isPerimeter(tx, ty);
                if (!perimeter) {
                    if (this.isHorizontalRun(tx, ty)) {
                        this.drawSprite('thin_h', tx * T, ty * T + 4, false);
                        // Close the T-junction where this run meets a vertical
                        // one. thin_h fills its tile edge to edge but thin_v is
                        // a 7px plank inset 4px, so it stops five pixels short
                        // of the next tile -- a hole in the wall, at exactly the
                        // corner where two rooms and a corridor meet. A second
                        // plank laid half a tile into the junction covers it.
                        for (const nx of [tx - 1, tx + 1]) {
                            if (this.isWall(nx, ty) && !this.isPerimeter(nx, ty) &&
                                !this.isHorizontalRun(nx, ty)) {
                                this.drawSprite('thin_h', tx * T + (nx < tx ? -8 : 8),
                                                ty * T + 4, false);
                            }
                        }
                    } else {
                        this.drawSprite('thin_v', tx * T + 4, ty * T, false);
                    }
                    continue;
                }

                let name;
                if (ty === 0) name = 'w_top';
                else if (ty === 1) name = 'w_upper';
                else if (ty === 2) name = 'w_wainscot';
                else name = 'w_top';
                this.drawSprite(name, tx * T, ty * T, false);

                // Interior walls are seen purely from above, so the flat top
                // tile alone reads as a painted stripe on the floor. The
                // reference gives every wall run a lit north edge and a cast
                // shadow on its south side, which is what makes it read as
                // something standing up. Only edges facing open floor get one.
                if (ty > 2 && perimeter) {
                    if (!this.isSolid(tx, ty - 1)) {
                        ctx.fillStyle = WALL_EDGE_LIT;
                        ctx.fillRect(tx * T, ty * T, T, 1);
                    }
                    if (!this.isSolid(tx, ty + 1)) {
                        ctx.fillStyle = WALL_EDGE_SHADE;
                        ctx.fillRect(tx * T, (ty + 1) * T - 2, T, 2);
                        ctx.fillStyle = WALL_CAST;
                        ctx.fillRect(tx * T, (ty + 1) * T, T, 3);
                    }
                    if (!this.isSolid(tx - 1, ty)) {
                        ctx.fillStyle = WALL_EDGE_SHADE;
                        ctx.fillRect(tx * T, ty * T, 1, T);
                    }
                    if (!this.isSolid(tx + 1, ty)) {
                        ctx.fillStyle = WALL_EDGE_LIT;
                        ctx.fillRect((tx + 1) * T - 1, ty * T, 1, T);
                        ctx.fillStyle = WALL_CAST;
                        ctx.fillRect((tx + 1) * T, ty * T, 3, T);
                    }
                }
            }
        }
    }

    /** Props and agents share one list so they occlude each other correctly. */
    drawDepthSorted(ctx) {
        const T = this.tile;
        const items = [];

        for (const p of this.map.props) {
            const f = this.frames[p.sprite];
            if (!f || p.layer === 'floor') continue;   // rugs drew with the floor
            // `z` biases sort order without moving the sprite, so desk-top items
            // draw in front of the desk they share a baseline with.
            items.push({ kind: 'prop', p, baseline: (p.y + 1) * T + (p.z || 0) * 0.01 });
        }
        for (const a of this.agents) {
            items.push({ kind: 'agent', a, baseline: a.y * T });
        }
        for (const d of this.doors) {
            items.push({ kind: 'door', d, baseline: (d.y + 1) * T });
        }
        items.sort((m, n) => m.baseline - n.baseline);

        for (const it of items) {
            if (it.kind === 'prop') {
                const p = it.p;
                this.drawSprite(p.sprite, (p.x + 0.5) * T, (p.y + 1) * T + (p.dy || 0));
            } else if (it.kind === 'door') {
                const d = it.d;
                // The strip runs closed -> fully open over DOOR_OPEN_FRAMES; the
                // frames past that are the closing half, which we never need
                // because `open` is eased in both directions already.
                const f = Math.round(d.open * (DOOR_OPEN_FRAMES - 1));
                // `kind` lets a door name its own leaf family. The entrance
                // sits in the 16px shell course rather than a 7px partition,
                // so it needs leaves cut from that wall instead.
                const base = d.kind || (d.v ? 'door_slide_v' : 'door_slide');
                const sprite = `${base}_${f}`;
                const cx = d.v ? (d.x + 0.5) * T : (d.x + 1) * T;
                this.drawSprite(sprite, cx, (d.y + 1) * T + (d.dy || 0));
            } else {
                this.drawAgent(ctx, it.a);
            }
        }
    }

    drawAgent(ctx, a) {
        const T = this.tile;
        const clips = this.characters[a.sprite];
        if (!clips) return;

        const moving = !!a.path;
        const clip = `${moving ? 'walk' : 'idle'}_${a.facing}`;
        const frames = clips[clip] || clips['idle_down'];
        if (!frames || !frames.length) return;

        const idx = moving ? (a.walkFrame % frames.length) : 0;
        const px = a.x * T;
        // Working at a desk, the figure bobs one world pixel. The atlas has no
        // typing clip, and frame 0 held still read as paused, not busy.
        const working = !moving && (a.state === 'active' || a.state === 'tool_calling');
        const bob = working && (Math.floor(performance.now() / 260 + (a.phase ??= Math.random() * 2)) % 2) ? 1 : 0;
        const py = a.y * T;

        // Soft contact shadow so characters sit on the floor instead of floating.
        ctx.save();
        ctx.globalAlpha = 0.22;
        ctx.fillStyle = '#000';
        ctx.beginPath();
        ctx.ellipse(px, py - 1, 5, 2.2, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        this.drawSprite(frames[idx], px, py - bob, true,
                        a.hue ? this.tintedAtlas(a.hue) : null);

        // The visitor badge. Two pixels of lanyard and a pale card on the
        // chest -- small, but it is the whole point of drawing a contractor
        // differently: someone hired twenty minutes ago must not look like
        // staff who have been here all along.
        if (a.spawned) {
            ctx.save();
            ctx.fillStyle = '#e9e3cf';
            ctx.fillRect(Math.round(px + 1), Math.round(py - 18), 4, 5);
            ctx.fillStyle = '#8b5cf6';
            ctx.fillRect(Math.round(px + 1), Math.round(py - 18), 4, 1.5);
            ctx.strokeStyle = 'rgba(30,30,40,0.55)';
            ctx.lineWidth = 0.5;
            ctx.strokeRect(Math.round(px + 1), Math.round(py - 18), 4, 5);
            ctx.restore();
        }

        if (a.id === this.selectedAgentId) {
            ctx.save();
            ctx.strokeStyle = '#f8c873';
            ctx.lineWidth = 1;
            ctx.globalAlpha = 0.9;
            ctx.strokeRect(Math.round(px - 9), Math.round(py - 33), 18, 34);
            ctx.restore();
        }
    }

    drawOrbs(ctx) {
        const T = this.tile;
        ctx.save();
        for (const p of this.particles) {
            ctx.globalAlpha = Math.max(0, p.life);
            ctx.fillStyle = p.c;
            ctx.fillRect(p.x * T, p.y * T, 1.5, 1.5);
        }
        ctx.restore();
    }

    /* =======================================================================
       Lighting.

       The reference is not flat-lit: desks sit in warm pools and the corners
       fall away to near-dark, and that falloff is most of what reads as "cozy".

       It is baked once into an offscreen light map at world resolution and then
       multiplied over the finished frame. Baking matters — evaluating sixteen
       radial gradients per frame costs more than the rest of the renderer put
       together, and the lights never move.
       ======================================================================= */

    buildLightMap() {
        const c = document.createElement('canvas');
        c.width = this.baseWidth;
        c.height = this.baseHeight;
        const g = c.getContext('2d');

        // The floor of the range: what an unlit corner is multiplied by. Blue is
        // pulled down hardest so shadow stays warm instead of going grey.
        g.fillStyle = AMBIENT;
        g.fillRect(0, 0, c.width, c.height);

        // Additive, so overlapping pools brighten toward the cap rather than
        // painting over one another.
        g.globalCompositeOperation = 'lighter';
        const T = this.tile;
        for (const L of this.map.lights) {
            const cx = L.x * T, cy = L.y * T, r = L.r * T;
            const grad = g.createRadialGradient(cx, cy, 0, cx, cy, r);
            const a = L.i === undefined ? 1 : L.i;
            // Two inner stops: a flat-ish core, then a long tail. A single
            // linear stop reads as a hard disc edge at this scale.
            grad.addColorStop(0,    `rgba(${LIGHT_R},${LIGHT_G},${LIGHT_B},${a})`);
            grad.addColorStop(0.30, `rgba(${LIGHT_R},${LIGHT_G},${LIGHT_B},${a * 0.72})`);
            grad.addColorStop(0.65, `rgba(${LIGHT_R},${LIGHT_G},${LIGHT_B},${a * 0.26})`);
            grad.addColorStop(1,    `rgba(${LIGHT_R},${LIGHT_G},${LIGHT_B},0)`);
            g.fillStyle = grad;
            g.beginPath();
            g.arc(cx, cy, r, 0, Math.PI * 2);
            g.fill();
        }
        return c;
    }

    /**
     * The additive layer: fixture glow cones washing down the wall, and a
     * halo around each floor/table lamp. Baked like the light map.
     */
    buildGlowMap() {
        const c = document.createElement('canvas');
        c.width = this.baseWidth;
        c.height = this.baseHeight;
        const g = c.getContext('2d');
        const T = this.tile;
        const [gr, gg, gb] = GLOW_COLOR;

        // Wall fixtures: an ellipse of glow spreading down from the rail. The
        // reference's cones are wider than tall, brightest just under the
        // fixture, gone by the wainscot line.
        for (const f of (this.map.fixtures || [])) {
            const cx = (f.x + 0.5) * T;
            const cy = 1.6 * T;                      // just under the rail
            // Reference peaks at (247,185,99) directly under a fixture -- a
            // hot core about a third of the cone wide, then a fast falloff.
            const grad = g.createRadialGradient(cx, cy, 0, cx, cy, 2.6 * T);
            grad.addColorStop(0,    `rgba(${gr},${gg},${gb},1)`);
            grad.addColorStop(0.3,  `rgba(${gr},${gg},${gb},0.45)`);
            grad.addColorStop(0.6,  `rgba(${gr},${gg},${gb},0.15)`);
            grad.addColorStop(1,    `rgba(${gr},${gg},${gb},0)`);
            g.fillStyle = grad;
            g.save();
            g.translate(cx, cy);
            g.scale(1, 0.75);                        // squash: wider than tall
            g.beginPath();
            g.arc(0, 0, 2.6 * T, 0, Math.PI * 2);
            g.fill();
            g.restore();

            // Near-white peak right at the fixture, ~1 tile wide.
            const hot = g.createRadialGradient(cx, cy - 4, 0, cx, cy - 4, 0.9 * T);
            hot.addColorStop(0, 'rgba(255,232,170,0.9)');
            hot.addColorStop(1, 'rgba(255,232,170,0)');
            g.fillStyle = hot;
            g.beginPath();
            g.arc(cx, cy - 4, 0.9 * T, 0, Math.PI * 2);
            g.fill();
        }

        // Lamps get a small round halo so they read as switched on.
        for (const p of this.map.props) {
            if (!/lamp/.test(p.sprite)) continue;
            const f2 = this.frames[p.sprite] || { h: 32 };
            const cx = (p.x + 0.5) * T;
            const cy = (p.y + 1) * T - f2.h + 6;     // around the shade
            const grad = g.createRadialGradient(cx, cy, 0, cx, cy, 1.6 * T);
            grad.addColorStop(0,   `rgba(${gr},${gg},${gb},0.5)`);
            grad.addColorStop(1,   `rgba(${gr},${gg},${gb},0)`);
            g.fillStyle = grad;
            g.beginPath();
            g.arc(cx, cy, 1.6 * T, 0, Math.PI * 2);
            g.fill();
        }
        return c;
    }

    /** The fixtures themselves: a dark puck with a lit face, on the rail. */
    drawFixtures(ctx) {
        const T = this.tile;
        for (const f of (this.map.fixtures || [])) {
            const cx = (f.x + 0.5) * T;
            const y = 1.0 * T;
            ctx.fillStyle = FIXTURE_BODY;
            ctx.fillRect(Math.round(cx - 3), y - 2, 6, 3);
            ctx.fillStyle = FIXTURE_LIT;
            ctx.fillRect(Math.round(cx - 2), y, 4, 2);
        }
    }

    applyLighting(ctx) {
        if (!this.map.lights || !this.map.lights.length) return;
        if (!this.lightMap) this.lightMap = this.buildLightMap();
        if (!this.glowMap) this.glowMap = this.buildGlowMap();

        this.drawFixtures(ctx);

        ctx.save();
        // Smoothing back on for this one draw: the light map is a gradient, and
        // nearest-neighbour upscaling of a gradient produces visible banding
        // rings. Everything else in the frame stays pixelated.
        ctx.imageSmoothingEnabled = true;
        ctx.globalCompositeOperation = 'multiply';
        ctx.drawImage(this.lightMap, 0, 0);
        // Emitted light goes on after the shadow pass, so the cones and halos
        // sit on top of the darkness instead of being darkened with it.
        ctx.globalCompositeOperation = 'lighter';
        ctx.drawImage(this.glowMap, 0, 0);
        ctx.restore();
        ctx.imageSmoothingEnabled = false;
    }

    /* =======================================================================
       Room shell.

       The reference does not draw the office as one filled rectangle. It draws
       three blocks — bullpen, boss office, lounge — floating on black, each one
       rimmed in orange over a dark edge, with the dividing walls reading as
       voids rather than as textured slabs. Doorways bridge the voids.

       This is built once as a Path2D from the map's own room/doorway data, so
       moving a wall in office_map.js moves the shell with it.
       ======================================================================= */

    buildShell() {
        const T = this.tile;
        const r = SHELL_RADIUS;
        const path = new Path2D();
        // One solid rectangle. The earlier reference floated three blocks on
        // black; this one is a single building whose rooms are divided by real
        // walls, so there are no voids to cut and the whole floor plan is one
        // clipped region.
        this.roundRectPath(path, 0, 0, this.baseWidth, this.baseHeight, r);
        return path;
    }

    buildShellBlocks() {
        const T = this.tile;
        const r = SHELL_RADIUS;
        const path = new Path2D();

        // Blocks are the map's rooms grown out to the centre of their wall ring,
        // so the rim lands on the wall rather than inside the furnished floor.
        for (const b of this.map.blocks) {
            this.roundRectPath(path, b.x * T, b.y * T, b.w * T, b.h * T, r);
        }
        // Doorways bridge two blocks. Their inner edges overlap the blocks and
        // would otherwise be stroked as visible seams — drawShell paints the
        // rim first and the floor over it, which buries them.
        for (const d of this.map.doorways) {
            path.rect(d.x * T, d.y * T, (d.w || 1) * T, (d.h || 1) * T);
        }
        return path;
    }

    /**
     * Rim first, world second.
     *
     * Stroking a union of overlapping rectangles paints the internal edges too.
     * Rather than compute a true outline, both rim passes go down before the
     * floor, and the floor — clipped to the same path — covers every stroke
     * that falls inside the shell. What survives is the outer half only: a dark
     * edge with an orange rim inside it, seamless across the doorways.
     */
    drawShellRim(ctx, shell) {
        ctx.save();
        ctx.lineJoin = 'round';

        // Layered bevel, outermost first. Each pass is narrower than the last,
        // so what survives outside the floor is a dark outer line, a warm mid
        // band and a lit inner edge -- the thick decorative border the
        // reference frames the whole building with.
        const bands = [
            [SHELL_RIM * 2 + 14, FRAME_OUTER],
            [SHELL_RIM * 2 + 8,  FRAME_MID],
            [SHELL_RIM * 2 + 3,  FRAME_LIT],
            [SHELL_RIM * 2,      FRAME_INNER],
        ];
        for (const [w, colour] of bands) {
            ctx.strokeStyle = colour;
            ctx.lineWidth = w;
            ctx.stroke(shell);
        }
        ctx.restore();
    }

    /* =======================================================================
       Speech bubbles.

       These are drawn in SCREEN space, after the world transform is popped.
       The reference art renders its labels at native resolution with smooth
       type; drawing them inside the world transform instead multiplies a 7px
       font by the zoom, which is both blurry and far too small. Positions are
       still world-derived — only the glyphs escape the scaling.
       ======================================================================= */

    /** World tile coords -> screen pixels. */
    toScreen(tx, ty) {
        return [this.offsetX + tx * this.tile * this.zoom,
                this.offsetY + ty * this.tile * this.zoom];
    }

    /**
     * ctx.roundRect is not in every engine we might run under.
     *
     * `target` is a context or a Path2D. Path2D has no beginPath, and calling
     * it on a Path2D would be wrong anyway -- buildShell appends several
     * subpaths to one object.
     */
    roundRectPath(target, x, y, w, h, r) {
        const ctx = target;
        r = Math.min(r, w / 2, h / 2);
        if (ctx.beginPath) ctx.beginPath();
        if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y,     x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x,     y + h, r);
        ctx.arcTo(x,     y + h, x,     y,     r);
        ctx.arcTo(x,     y,     x + w, y,     r);
        ctx.closePath();
    }

    /** Break `text` so no line exceeds `maxW` at the current font. */
    wrapText(ctx, text, maxW) {
        const words = String(text).split(/\s+/);
        const lines = [];
        let line = '';
        for (const word of words) {
            const test = line ? line + ' ' + word : word;
            if (line && ctx.measureText(test).width > maxW) {
                lines.push(line);
                line = word;
            } else {
                line = test;
            }
        }
        if (line) lines.push(line);
        return lines.slice(0, 3);
    }

    drawOverlay(ctx) {
        // Measured off the reference: its plate type is ~16px tall in an office
        // panel 955px wide, i.e. ~9.5px per unit of world zoom. Clamped so a
        // small window stays readable and a large one stays sane.
        const fs = Math.max(11, Math.min(22, Math.round(9.5 * this.zoom)));

        ctx.save();
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';

        const now = Date.now();
        for (const a of this.agents) {
            const [sx, sy] = this.toScreen(a.x, a.y);
            // The character sprite stands ~32 world px tall; clear its head.
            const headY = sy - 34 * this.zoom;
            if (a.speech) {
                this.drawBubble(ctx, sx, headY, a.speech.text, fs);
                continue;
            }
            const tag = this.statusTag(a, now);
            if (tag) this.drawTag(ctx, sx, headY - 4, tag, fs);
        }
        ctx.restore();
    }

    /**
     * What the tag over an agent should say, most urgent first. Null when the
     * agent has nothing worth a label -- an idle office should be quiet.
     */
    statusTag(a, now) {
        const since = now - (a.stateAt || 0);
        if (a.state === 'error' && since < DONE_TAG_MS * 2) {
            return { text: 'Step failed', dot: TAG_BAD, ink: '#ffc2b7' };
        }
        if (a.queued) {
            const behind = a.queued.by ? ` · behind ${a.queued.by}` : '';
            return { text: `Waiting for ${a.queued.label}${behind}`, dot: TAG_ACCENT, hollow: true };
        }
        if (a.tool) {
            return { text: a.tool.name.replace(/_/g, ' '), dot: TAG_OK, pulse: true };
        }
        if (a.state === 'thinking') return { dots: true, dot: TAG_ACCENT };
        if (a.state === 'done' && since < DONE_TAG_MS) {
            return { text: 'Done', dot: TAG_OK, check: true };
        }
        if (a.heading && (a.path || now < a.heading.until)) {
            return { text: `→ ${a.heading.label}`, muted: true };
        }
        return null;
    }

    /** A small dark pill with a status dot, drawn in screen space. */
    drawTag(ctx, sx, bottomY, tag, fs) {
        const tfs = Math.max(10, Math.round(fs * 0.78));
        const h = Math.round(tfs * 1.75);
        const padX = Math.round(tfs * 0.75);
        const dotR = Math.max(2.5, tfs * 0.26);
        const gap = Math.round(tfs * 0.5);

        ctx.save();
        ctx.font = `500 ${tfs}px ${LABEL_FONT}`;
        const label = tag.dots ? '' : String(tag.text || '');
        const textW = tag.dots ? tfs * 1.6 : ctx.measureText(label).width;
        const hasDot = Boolean(tag.dot) && !tag.dots;
        const w = Math.ceil(padX * 2 + textW + (hasDot ? dotR * 2 + gap : 0));
        const x = Math.round(sx - w / 2);
        const y = Math.round(bottomY - h);

        ctx.shadowColor = 'rgba(0, 0, 0, 0.45)';
        ctx.shadowBlur = 8;
        ctx.shadowOffsetY = 2;
        this.roundRectPath(ctx, x, y, w, h, h / 2);
        ctx.fillStyle = TAG_BG;
        ctx.fill();
        ctx.shadowColor = 'transparent';
        ctx.lineWidth = 1;
        ctx.strokeStyle = TAG_EDGE;
        ctx.stroke();

        const cy = y + h / 2;
        let cx = x + padX;
        if (hasDot) {
            const t = performance.now() / 1000;
            const alpha = tag.pulse ? 0.55 + 0.45 * Math.sin(t * 5) : 1;
            ctx.globalAlpha = alpha;
            ctx.beginPath();
            ctx.arc(cx + dotR, cy, dotR, 0, Math.PI * 2);
            if (tag.hollow) {
                ctx.strokeStyle = tag.dot;
                ctx.lineWidth = 1.5;
                ctx.stroke();
            } else {
                ctx.fillStyle = tag.dot;
                ctx.fill();
            }
            ctx.globalAlpha = 1;
            cx += dotR * 2 + gap;
        }

        if (tag.dots) {
            // Three dots rising in turn: thinking, with nothing to say yet.
            const t = performance.now() / 1000;
            for (let i = 0; i < 3; i++) {
                const lift = Math.max(0, Math.sin(t * 6 - i * 0.9)) * tfs * 0.18;
                ctx.globalAlpha = 0.45 + 0.55 * Math.max(0, Math.sin(t * 6 - i * 0.9));
                ctx.fillStyle = tag.dot;
                ctx.beginPath();
                ctx.arc(cx + tfs * 0.28 + i * tfs * 0.52, cy - lift, tfs * 0.16, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.globalAlpha = 1;
        } else {
            ctx.fillStyle = tag.ink || (tag.muted ? TAG_MUTED : TAG_FG);
            ctx.textBaseline = 'middle';
            ctx.textAlign = 'left';
            ctx.fillText(label, cx, cy + 0.5);
        }
        ctx.restore();
    }

    /** White speech balloon, centred text, tail hanging toward the speaker. */
    drawBubble(ctx, sx, bottomY, text, fs) {
        const padX = Math.round(fs * 0.8);
        const padY = Math.round(fs * 0.5);
        const lh = Math.round(fs * 1.3);
        const tail = Math.round(fs * 0.62);

        ctx.font = `500 ${fs}px ${LABEL_FONT}`;
        const lines = this.wrapText(ctx, text, fs * 14);
        const textW = Math.max(...lines.map(l => ctx.measureText(l).width));

        const w = Math.ceil(textW) + padX * 2;
        const h = lh * lines.length + padY * 2;
        const x = Math.round(sx - w / 2);
        const y = Math.round(bottomY - tail - h);

        this.paintCallout(ctx, x, y, w, h, sx, BUBBLE_BG, BUBBLE_EDGE, fs, tail, 0.22);

        ctx.fillStyle = BUBBLE_FG;
        ctx.textAlign = 'center';
        lines.forEach((line, i) => {
            ctx.fillText(line, x + w / 2, y + padY + lh * (i + 1) - Math.round(fs * 0.3));
        });
        ctx.textAlign = 'left';
    }

    /**
     * Rounded panel plus a tail. The body is stroked first, then the tail is
     * filled and only its two sloping sides are stroked — stroking the tail as
     * a closed path would draw a line straight across the panel's bottom edge.
     *
     * `tipFrac` places the tail across the panel's width (0 = left edge,
     * 1 = right) whenever the speaker falls outside the panel's span.
     */
    paintCallout(ctx, x, y, w, h, tipX, fill, edge, fs, tail, tipFrac) {
        const r = Math.round(fs * 0.55);
        const half = Math.max(3, Math.round(fs * 0.3));

        let base = tipX;
        const lo = x + r + half, hi = x + w - r - half;
        if (base < lo || base > hi) base = x + w * tipFrac;
        base = Math.max(lo, Math.min(hi, base));

        ctx.save();
        ctx.lineJoin = 'round';
        ctx.lineWidth = Math.max(2, Math.round(this.zoom * 1.1));

        ctx.shadowColor = 'rgba(0,0,0,0.45)';
        ctx.shadowBlur = Math.round(fs * 0.5);
        ctx.shadowOffsetY = Math.max(1, Math.round(fs * 0.15));

        this.roundRectPath(ctx, x, y, w, h, r);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.shadowColor = 'transparent';
        ctx.strokeStyle = edge;
        ctx.stroke();

        const yb = y + h;
        ctx.beginPath();
        ctx.moveTo(base - half, yb - 1);
        ctx.lineTo(base - half + tail * 0.15, yb + tail);
        ctx.lineTo(base + half, yb - 1);
        ctx.closePath();
        ctx.fillStyle = fill;
        ctx.fill();

        ctx.beginPath();
        ctx.moveTo(base - half, yb - 1);
        ctx.lineTo(base - half + tail * 0.15, yb + tail);
        ctx.lineTo(base + half, yb - 1);
        ctx.strokeStyle = edge;
        ctx.stroke();
        ctx.restore();
    }

    /* ===================== input ===================== */

    handleClick(e) {
        const rect = this.canvas.getBoundingClientRect();
        const mx = (e.clientX - rect.left - this.offsetX) / this.zoom / this.tile;
        const my = (e.clientY - rect.top - this.offsetY) / this.zoom / this.tile;

        let best = null;
        let bestD = 1.4;
        for (const a of this.agents) {
            const d = Math.hypot(a.x - mx, a.y - my - 0.8);
            if (d < bestD) { bestD = d; best = a; }
        }
        if (best) {
            this.selectedAgentId = best.id;
            this.showSpeech(best.id, `${best.name}: online.`);
            if (window.retroHUD) {
                window.retroHUD.selectAgent(best.id);
            }
        }
    }
}
