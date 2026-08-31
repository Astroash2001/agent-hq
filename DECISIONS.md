# Decisions

Choices already made and the reasoning behind them, so they don't get re-litigated.
Several of these look wrong at first glance and are deliberate.

Context: [[README]] · structure in [[ARCHITECTURE]] · versions in [[STACK]] ·
open items in [[TODO]].

---

## Backend

### An event bus sits between the agents and the socket

`orchestrator`, `base_agent` and `tool_registry` never touch a WebSocket. They
`event_bus.emit(...)`; `main.py` registers itself with `subscribe_all` and
broadcasts. Adding a second consumer — a log file, a recorder, a second UI — is one
`subscribe` call and touches nothing else.

The bus also keeps a 200-event rolling history, which is what makes reconnects
work: a browser that drops gets the last 30 events replayed and is immediately back
in sync rather than staring at an empty office.

### The LLM is optional, and there is a real simulator behind it

`base_agent._call_llm` tries LiteLLM only when an API key is present, with a 12s
timeout, and **falls through to `_simulate_specialist_action` on any exception**.

The simulator is not a stub — it inspects the agent id and task text and returns a
plausible tool call on the first pass, then a formatted result on the second, so
the full ReAct loop, tool execution, event stream and UI animation all exercise
identically. This means the office demos reliably with no key, no network and no
spend, and a provider outage degrades to a working demo instead of a dead screen.

The cost is that token counts on the simulator path are **estimates**, not real
usage. Tracked in [[TODO]].

### LiteLLM rather than a provider SDK

One call shape for Gemini, OpenAI and Anthropic. Switching models is a string in
`config.py`. `drop_params = True` absorbs the parameter differences between
providers rather than branching on them.

### Planning is an LLM call, with the keyword buckets kept as the floor

`planner.py` asks the model for a DAG and validates it hard. The four intent
buckets it replaced are still there as `heuristic_plan`, and they are still what
runs with no key, on a provider outage, or when the model returns something that
will not validate twice in a row.

Keeping both is the same bet `base_agent` already made: the interesting path is
the model, but the demo must never depend on it. Planning stays instant, free and
deterministic whenever it has to be.

### The office is a constraint, not a view

Tools are bound to stations and agents walk to them (`office.py`). This is the
one decision that looks like decoration and is not: a station holds one agent, so
two specialists that need the same tool serialise, and moving a station in the
map genuinely changes how long a mission takes.

The alternative — keeping tools callable from anywhere and animating the walk
afterwards — was rejected because it makes the floor plan cosmetic, which is what
every other project with a rendered agent office does. `spatial_enabled` turns it
off if the cost ever outweighs the point.

### Memory similarity is lexical, on purpose

`memory.py` scores with IDF-weighted token overlap rather than embeddings. It is
worse at recognising paraphrase, and it needs no model, no key, no extra
dependency and no network — which keeps the property that the whole system runs
with nothing installed. `_similarity` is the single function to replace when that
trade stops being worth it.

### Rerouting is announced, never silent

When memory overrides the planner's choice of specialist it emits
`route.changed` with both scores and a reason. An office that quietly reassigns
work is impossible to reason about; the whole point of the visualisation is that
you can see why something happened.

### Agent manifests carry presentation, not just prompts

`AgentManifest` holds `system_prompt` and `tools` alongside `name`, `plate_verb`,
`plate_icon` and `color`. One file defines both what an agent *is* and how it
appears in the office, so adding an agent doesn't mean editing two systems that can
drift apart.

### The backend serves the frontend

`main.py` mounts `frontend/` at `/`. Same origin means the WebSocket URL is derived
from `window.location` and there is no CORS or port configuration to get wrong.
The permissive CORS middleware is a dev convenience and should be tightened before
any deployment — [[TODO]].

---

## Frontend

### No framework and no build step

The UI is one canvas and a sidebar of DOM elements updated by direct manipulation.
A framework would add a build, a dependency tree and a virtual DOM to manage
roughly twenty elements, while the actual complexity — the renderer — lives in
canvas code a framework doesn't help with. Editing a file and refreshing is the
whole loop.

### The office floor plan is data, not code

`office_map.js` exports one `OFFICE_MAP` object. The renderer knows how to draw
*any* map; it knows nothing about this particular office. Moving a desk is editing
a number, and `check_layout.py` and `preview_map.py` both read the same file, so
validation and headless preview can never drift from what the browser draws.

### Labels render in screen space, everything else in world space

Plates and speech bubbles are drawn after the world transform is popped. Drawing
them inside it multiplied a 7px font by the zoom — blurry and far too small.
Positions are still world-derived; only the glyphs escape the scaling.

### Lighting is baked into two maps, not evaluated per frame

A *light map* (ambient plus additive radial pools) is composited with `multiply`;
a *glow map* (fixture cones, lamp halos) is composited with `lighter` **after**, so
emitted light sits on top of the shadow instead of being darkened with it.

Both are built once — sixteen radial gradients per frame cost more than the rest of
the renderer combined, and the lights never move. Image smoothing is re-enabled for
these two draws only, because nearest-neighbour upscaling of a gradient bands
visibly.

### The lighting values were solved, not eyeballed

The reference's floor runs `(56,25,10)` unlit to `(134,78,41)` lit. Our floor
paints at `(161,91,62)`, so the required multipliers are `(0.35,0.27,0.16)` and
`(0.83,0.86,0.66)` — a light map running `rgb(89,70,41)` to `rgb(212,219,168)`.
`LIGHT_G` exceeds `LIGHT_R` because the ambient is already red-heavy: reaching a
neutral-warm peak takes proportionally more green and blue.

### Desks are barely tinted, and that is the second attempt

The reference's desks are darker than *its* floor. Ours would be darker than a
floor that is already dark. Tinting to `0.58` and again to `0.72` both landed the
desk surface on the floor's own value and made the desks disappear.

Matching the reference properly needs the floor lightened to about `(198,126,84)`.
That was built and compared side by side: the lighter floor loses the warm dimness
the whole scene depends on, and the dark desks then vanish into the unlit corners
instead. Light desks on a dark floor keep both contrast and mood.

### The floor is synthesised, not from the pack

Every 48×48 block in `Room_Builder_Floors` was scored against the reference's
measured `(168,104,62)`, requiring warm hue and horizontal banding. The best match
is at `(128,192)` — but it is markedly more saturated and busier than the
reference's quiet planks. Measured, the synthesised block wins. The pack floors are
deliberately unused.

### Doors are generated from the wall they sit in

The pack's sliding-glass door was 32px deep with warm tan panels against a 10px
run of pale glazing — mismatched on both depth and material, and cropping fixes one
while worsening the other.

So door leaves are cut from the wall sprite itself at atlas build time. Same
source, same palette, same depth: it matches by construction, and if the wall
changes the door follows automatically. Each leaf gets a frame outline and a pull
handle, because cut straight from the wall a closed door was pixel-identical to it
— correct for glass, useless for a user.

### The atlas and its manifest share one cache-busting token

The browser caches them independently. After a rebuild it will happily pair a fresh
PNG with a stale JSON, and every frame then reads from the wrong rectangle — the
office renders as scrambled furniture. One shared `?v=` per load keeps the pair
consistent.

---

## Asset pipeline

### Sprites are picked by index, never by pixel rectangle

`index_pack.py` segments a sheet (or reads a pre-cropped Singles folder), writes a
JSON index of exact bounding boxes, and renders a numbered contact sheet. A pick in
`build_atlas.py` is a `(sheet, index)` pair; the box comes from the index.

Hand-reading coordinates off a 256×1248 sheet is how the atlas previously ended up
with clipped sofas and bookshelves missing their top shelf.

**Exception:** the wall and border sheets are one continuous atlas with no
transparent gutters — component segmentation fuses them into a single blob — so
those few tiles are cut by explicit coordinate, with the reasoning noted inline.

### The office-pack singles are trimmed on load

All 339 are padded into a uniform 32×48 frame. Untrimmed, every prop would sit up
to 40px above its own baseline. `pack_source.trim()` crops it, and `index_pack.py`
uses the *same* function so the contact sheets report the size the atlas will
actually get.

### LimeZu became the style anchor, replacing Pixel Life

The original `build_atlas.py` header explicitly rejected LimeZu as too soft-shaded
for a Pixel Life base. The reference the office is now built against *is* LimeZu's
style, so the anchor flipped. Older packs now fill only what LimeZu lacks — and
Modern Interiors is a *home* pack with no office equipment at all, which is why
Modern Office was a separate purchase.

### Some props are authored because no pack has them

Planter troughs, the 208px boardroom table, and the conference rug. Both packs top
out around 80px for a table; the largest rug anywhere is 56×58. Tiling a short one
leaves a seam every five tiles, and a row of single pots does not read like a hedge
trough. These are drawn in `gen_tiles.py` with deterministic per-pixel jitter so
they never read as a repeating pattern.

### Layout is validated geometrically, and the validator knows the exceptions

`check_layout.py` computes real pixel rectangles from the atlas and gates a
rebuild. It carries deliberate exemptions rather than false positives: wall-mounted
items share floor space with what stands below them; partition runs *are* walls, so
a room's side wall meeting the building's south wall is the correct join; and a
chair drawn up to a table's long edge is furniture, not a collision — a boardroom
seats seven a side and every one of them overlaps the slab.

### Verification is headless screenshots, not eyeballing the browser

`preview_map.py` renders without a browser; Playwright drives real Chromium for the
full page. Both are scriptable, so a change can be rendered, measured against the
reference numerically, and compared side by side in one command.
