# Stack

What is actually installed and used. Versions are from the live environment, not
from a lockfile — **there is no `requirements.txt`**, which is tracked in [[TODO]].

See [[ARCHITECTURE]] for how these fit together and [[DECISIONS]] for why several
of them were chosen over the obvious alternative.

---

## Backend — Python

| Package | Version | Used for |
|---|---|---|
| `fastapi` | 0.128.8 | HTTP + WebSocket app, static mount |
| `uvicorn` | 0.46.0 | ASGI server (`python main.py` runs it with `reload=True`) |
| `pydantic` | 2.12.5 | Event / Task / manifest models in `schemas.py` |
| `pydantic-settings` | 2.12.0 | `Settings` in `config.py`, `.env` loading |
| `litellm` | 1.81.10 | Provider-agnostic LLM calls in `base_agent.py` |
| `httpx` | 0.28.1 | Pulled by FastAPI's `TestClient` |
| `pytest` | 9.1.1 | 11 tests in `backend/tests/` |
| `pytest-asyncio` | 1.4.0 | Async test support |

Standard library carries more than usual by design: `asyncio` for the event bus,
`glob`/`json` for manifest discovery, `uuid` for task ids, `ast` for
`calculate`'s expression allowlist, `sqlite3` for `query_data`.

### Models

Configured in `backend/config.py`:

- default `gemini/gemini-2.5-flash`
- fallback `gemini/gemini-2.0-flash`
- `temperature` 0.2, `max_tokens` 2048, request timeout 12s

Keys are read from `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` and
mirrored into the process environment for LiteLLM. **All optional** — see the
simulator fallback in [[DECISIONS]].

Guardrails: `max_agent_steps` 8, `max_tokens_budget_per_task` 150000.

---

## Frontend — vanilla

**No framework, no bundler, no `package.json`.** Four scripts and one stylesheet,
loaded by `index.html` in dependency order.

| Thing | Notes |
|---|---|
| ES2020+ JavaScript | Classes, spread, optional chaining, template literals |
| Canvas 2D | The whole office renderer, including baked lighting |
| `Path2D` | Room shell clipping and the bevelled frame |
| WebSocket | Single connection to `/ws`, backoff reconnect |
| `marked` (CDN) | Renders the executive-memo Markdown |
| Google Fonts | `Geist` (interface), `Geist Mono` (figures, log), `Silkscreen` (wordmark only) |
| CSS custom properties | Palette in `:root`; office colours sampled from the reference art, chrome tokens layered on top of them |
| CSS Grid + Flexbox | Top bar over a two-column shell; sidebar sections divided by rules, not frames |
| Inline SVG sprite | One `<defs>` block of line icons, referenced with `<use>` |
| WebAudio | Optional two-note cue when a mission ends or a step fails; off by default |

The only runtime network dependencies are the `marked` CDN script and Google
Fonts. Everything else is local.

---

## Asset tooling — Python

Development-time only; none of this ships to the browser.

| Package | Version | Used for |
|---|---|---|
| `Pillow` | 12.1.1 | All sprite cutting, synthesis and atlas composition |
| `numpy` | 1.26.4 | Alpha masks, colour scoring, texture statistics |
| `scipy` | 1.15.3 | `ndimage.label` — connected-component sprite segmentation |
| `playwright` | 1.62.0 | Headless Chromium screenshots for visual verification |
| `node` | — | `preview_map.py` shells out to it to read `office_map.js` directly |

`node` is used only so the preview renderer consumes the *same* map file the
browser does, rather than a second copy that can drift.

---

## Art packs

Under `furniture/`. Nothing here is loaded at runtime — it is all baked into
`frontend/assets/generated/office_atlas.png`.

| Pack | Licence | Role |
|---|---|---|
| **Modern Interiors** (LimeZu, paid) | Commercial use with credit | Primary: walls, floors, kitchen, lounge, plants, borders, animated objects |
| **Modern Office – Revamped** (LimeZu, paid) | Commercial use with credit | Desks, monitors, office chairs, partitions, printers |
| Modern Interiors *Free* v2.2 | **Non-commercial only** | Superseded by the paid pack; still present in `frontend/assets/` |
| Pixel Life – Desk Essentials | — | Legacy desk items, largely superseded |
| Little Bits Office | — | Legacy props and the character walk cycles still in use |
| Top-Down Retro Interior | — | Legacy lounge/kitchen pieces |
| sc_indoor_plants_free | — | Desk plants |
| Office-Furniture-Pixel-Art | — | Legacy, mostly unused |

**Licence note:** the free Modern Interiors pack is non-commercial. The paid packs
allow commercial use with credit to LimeZu. If this ever ships commercially, the
remaining free-pack sprites must be confirmed replaced — tracked in [[TODO]].

---

## Generated output

| File | Contents |
|---|---|
| `frontend/assets/generated/office_atlas.png` | Single texture — 233 prop sprites + 140 character frames |
| `frontend/assets/generated/office_atlas.json` | Frame rectangles + character clip index |
| `frontend/assets/generated/floor_wood.png` | Four 16×48 plank variants |
| `frontend/assets/generated/authored/` | Individual synthesised props, picked up by the atlas builder |
| `tools/_index/*.json` + `.png` | Sprite indices and browsable contact sheets |

---

## Platform

Developed on Windows 11 with Git Bash and PowerShell available. Paths in the Python
tools use `os.path.join` and forward slashes throughout, so nothing is
Windows-locked, but this has not been run on Linux or macOS.
