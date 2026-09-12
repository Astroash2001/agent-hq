/**
 * AI Agent HQ — HUD controller.
 *
 * Drives the amber terminal sidebar and the agent card dock. Layout and
 * palette follow the reference art; the dock is styled to match it even though
 * the dock itself comes from the motion reference.
 */

/* Avatars come from the same generated atlas the office renders, so a face in
   the dock is the same character you see at the desk. Reading the old LimeZu
   sheets here made the two disagree (green-haired Adam in the dock, a
   brown-haired red suit at the desk) and cropped wrongly, since those sheets
   frame their characters differently. */
/* Prefixed because canvas.js declares its own ATLAS_MANIFEST at top level.
   These are classic scripts sharing one global lexical scope, so a duplicate
   `const` is a SyntaxError that silently kills this entire file -- and with it
   the dock and the whole sidebar. */
const HUD_ATLAS_MANIFEST = 'assets/generated/office_atlas.json';
const HUD_ATLAS_IMAGE = 'assets/generated/office_atlas.png';
/* Portraits are pre-cropped to the silhouette (14x17) by tools/build_atlas.py,
   so 3x gives a 42x51 face that fills its box with no dead margin. */
const AVATAR_ZOOM = 3;

const DEFAULT_AGENTS = [
    { id: 'orchestrator', name: 'Boss Dave', verb: 'Planning',    icon: '📈', sprite: 'boss_dave' },
    { id: 'researcher',   name: 'Alice',     verb: 'Querying DB', icon: '⚡', sprite: 'alice'     },
    { id: 'data_analyst', name: 'Bob',       verb: 'Analysing',   icon: '🗄️', sprite: 'bob'       },
    { id: 'coder',        name: 'Charlie',   verb: 'Generating',  icon: '💡', sprite: 'charlie'   },
    { id: 'creative',     name: 'Eve',       verb: 'Generating',  icon: '💡', sprite: 'eve'       }
];

const GAUGE_SEGMENTS = 20;

class RetroHUD {
    constructor() {
        this.terminalLogs   = document.getElementById('terminalLogs');
        this.tokenUsageValue = document.getElementById('tokenUsageValue');
        this.tokenFuelGauge = document.getElementById('tokenFuelGauge');
        this.agentDock      = document.getElementById('agentDock');
        this.planCard       = document.getElementById('planCard');
        this.planNodes      = [];   // last plan, redrawn when the overlay opens
        this.planSource     = document.getElementById('planSource');
        this.executiveMemoCard = document.getElementById('executiveMemoCard');
        this.memoBodyContent   = document.getElementById('memoBodyContent');

        this.currentTokens = 0;
        this.maxTokens = 10000;
        this.agents = new Map();
        this.selectedAgentId = null;

        // Per-step marks that arrive on their own events rather than in the
        // DAG payload: a reroute (route.changed) and a review verdict
        // (step.critiqued). Kept here because dag.update redraws the whole
        // graph from scratch, so a badge painted onto the SVG would be wiped
        // by the next status change.
        this.stepBadges = new Map();    // step_id -> { routed, critique }

        // Live mission telemetry, rebuilt from every dag.update.
        this.cost = {
            startedAt: 0, costUsd: 0, tokens: 0, estimated: false,
            steps: [], byAgent: new Map(), running: false,
        };
        this.costCard    = document.getElementById('costCard');
        this._costTimer  = null;

        this.atlas = null;              // { size, frames, characters }

        this.buildGauge();
        this.registerAgents(DEFAULT_AGENTS);
        this.initEventListeners();
        this.loadAtlas();
    }

    /** The manifest may land after the dock is built, so re-skin on arrival. */
    loadAtlas() {
        fetch(HUD_ATLAS_MANIFEST)
            .then(r => r.json())
            .then(m => {
                this.atlas = m;
                this.applyAvatars();
            })
            .catch(() => {
                this.addTerminalLog(
                    'Avatar atlas missing — run: python tools/build_atlas.py', 'ERROR');
            });
    }

    /** Resolve an agent id to its atlas character key. */
    characterKey(agentId) {
        if (typeof OFFICE_MAP !== 'undefined' && OFFICE_MAP.agentSprites[agentId]) {
            return OFFICE_MAP.agentSprites[agentId];
        }
        // A contractor is in neither list -- it was hired after both were
        // written -- so its own manifest is the only thing that knows which
        // character it borrowed. Without this its dock card has no avatar.
        const registered = this.agents.get(agentId);
        if (registered && registered.sprite) return registered.sprite;
        const found = DEFAULT_AGENTS.find(a => a.id === agentId);
        return found ? found.sprite : null;
    }

    /**
     * Point each avatar at its portrait frame inside the atlas.
     *
     * Uses the pre-cropped `portraits` entry rather than slicing the full
     * idle frame here: that frame has transparent margins (1px each side, 2px
     * top), which left the face small and off-centre in its box. Falls back to
     * the idle frame only if an older atlas has no portraits.
     *
     * Box dimensions come from the manifest, so if the crop ever changes the
     * layout follows without touching the CSS.
     */
    applyAvatars() {
        if (!this.atlas || !this.agentDock) return;
        const [aw, ah] = this.atlas.size;

        this.agentDock.querySelectorAll('.dock-card').forEach(card => {
            const key = this.characterKey(card.dataset.agentId);
            if (!key) return;

            const portraits = this.atlas.portraits || {};
            let frame = portraits[key] && this.atlas.frames[portraits[key]];
            if (!frame) {
                const clips = this.atlas.characters[key];
                const fallback = clips && clips.idle_down && clips.idle_down[0];
                frame = fallback && this.atlas.frames[fallback];
            }
            if (!frame) return;

            const el = card.querySelector('.dock-avatar');
            if (!el) return;
            el.style.backgroundImage = `url("${HUD_ATLAS_IMAGE}")`;
            el.style.backgroundSize = `${aw * AVATAR_ZOOM}px ${ah * AVATAR_ZOOM}px`;
            el.style.backgroundPosition =
                `${-frame.x * AVATAR_ZOOM}px ${-frame.y * AVATAR_ZOOM}px`;
            el.style.width = `${frame.w * AVATAR_ZOOM}px`;
            el.style.height = `${frame.h * AVATAR_ZOOM}px`;
        });
    }

    /* --- construction ---------------------------------------------------- */

    buildGauge() {
        if (!this.tokenFuelGauge) return;
        this.tokenFuelGauge.innerHTML = '';
        for (let i = 0; i < GAUGE_SEGMENTS; i++) {
            const seg = document.createElement('div');
            seg.className = 'eq-seg';
            this.tokenFuelGauge.appendChild(seg);
        }
        this.updateTokens({ total_tokens: 0, max_tokens: this.maxTokens });
    }

    /**
     * Build the dock from agent manifests. Accepts either the backend shape
     * (plate_verb / plate_icon) or the local default shape (verb / icon).
     */
    registerAgents(manifests) {
        if (!this.agentDock || !Array.isArray(manifests) || !manifests.length) return;

        this.agentDock.innerHTML = '';
        this.agents.clear();

        manifests.forEach(m => {
            const agent = {
                id: m.id,
                name: m.name || m.id,
                verb: m.plate_verb || m.verb || 'Idle',
                icon: m.plate_icon || m.icon || '',
                sprite: m.sprite || this.spriteFor(m.id),
                state: 'idle',
                cwd: m.cwd || '~/agent-hq',
                progress: 0
            };
            this.agents.set(agent.id, agent);
            this.agentDock.appendChild(this.buildDockCard(agent));
        });

        this.applyAvatars();            // no-op until the atlas has loaded
    }

    spriteFor(id) {
        const found = DEFAULT_AGENTS.find(a => a.id === id);
        return found ? found.sprite : 'Adam';
    }

    /**
     * Add one specialist hired mid-mission to the dock, without rebuilding it.
     *
     * `registerAgents` clears the dock and starts over, which would throw away
     * the live state of everyone already working -- and a contractor arrives
     * in the middle of a mission, which is precisely when that state matters.
     */
    addAgentCard(manifest) {
        if (!this.agentDock || !manifest || !manifest.id) return;
        if (this.agents.has(manifest.id)) return;

        const agent = {
            id: manifest.id,
            name: manifest.name || manifest.id,
            verb: manifest.plate_verb || 'Contracting',
            icon: manifest.plate_icon || '🪪',
            sprite: manifest.sprite || this.spriteFor(manifest.id),
            state: 'idle',
            cwd: manifest.cwd || '~/agent-hq',
            progress: 0,
            spawned: true
        };
        this.agents.set(agent.id, agent);
        const card = this.buildDockCard(agent);
        // Marked in the DOM so the stylesheet can set a contractor apart
        // without the dock having to know what a contractor is.
        card.dataset.spawned = 'true';
        card.title = manifest.role
            ? `${manifest.role} — hired for this mission`
            : 'Hired for this mission';
        this.agentDock.appendChild(card);
        this.applyAvatars();
    }

    /** End every contract: the dock goes back to the permanent staff. */
    removeSpawnedCards() {
        this.agents.forEach((agent, id) => {
            if (!agent.spawned) return;
            const card = this.agentDock?.querySelector(`[data-agent-id="${id}"]`);
            if (card) card.remove();
            this.agents.delete(id);
        });
    }

    buildDockCard(agent) {
        const card = document.createElement('div');
        card.className = 'dock-card';
        card.setAttribute('role', 'listitem');
        card.dataset.agentId = agent.id;
        card.dataset.state = agent.state;

        const avatar = document.createElement('div');
        avatar.className = 'dock-avatar';   // filled in by applyAvatars()

        const main = document.createElement('div');
        main.className = 'dock-main';
        main.innerHTML =
            `<div class="dock-row">` +
                `<span class="dock-name"></span>` +
                `<span class="dock-status"></span>` +
            `</div>` +
            `<div class="dock-path"></div>` +
            `<div class="dock-progress"><i></i></div>`;

        main.querySelector('.dock-name').textContent = agent.name;
        main.querySelector('.dock-status').textContent = agent.state;
        main.querySelector('.dock-path').textContent = agent.cwd;

        card.append(avatar, main);
        card.addEventListener('click', () => this.selectAgent(agent.id));
        return card;
    }

    /* --- agent state ----------------------------------------------------- */

    cardFor(agentId) {
        return this.agentDock
            ? this.agentDock.querySelector(`.dock-card[data-agent-id="${agentId}"]`)
            : null;
    }

    setAgentState(agentId, state, detail = {}) {
        const agent = this.agents.get(agentId);
        const card = this.cardFor(agentId);
        if (!agent || !card) return;

        agent.state = state;
        card.dataset.state = state;
        card.querySelector('.dock-status').textContent = state;

        if (detail.cwd) {
            agent.cwd = detail.cwd;
            card.querySelector('.dock-path').textContent = detail.cwd;
        }
        if (typeof detail.progress === 'number') {
            agent.progress = Math.max(0, Math.min(1, detail.progress));
        } else {
            agent.progress = state === 'done' ? 1 : (state === 'idle' ? 0 : agent.progress);
        }
        card.querySelector('.dock-progress > i').style.width = `${agent.progress * 100}%`;
    }

    selectAgent(agentId) {
        this.selectedAgentId = agentId;
        if (this.agentDock) {
            this.agentDock.querySelectorAll('.dock-card').forEach(c => {
                c.dataset.selected = String(c.dataset.agentId === agentId);
            });
        }
        const agent = this.agents.get(agentId);
        if (agent) {
            this.addTerminalLog(`Selected ${agent.name}`, 'INFO');
        }
        if (window.officeCanvas && window.officeCanvas.focusAgent) {
            window.officeCanvas.focusAgent(agentId);
        }
    }

    /** Kept for app.js compatibility — highlights the dock card instead of a DAG node. */
    /**
     * Draw the planner's DAG.
     *
     * plan.created arrives once with the whole plan; dag.update arrives again
     * after every step with the statuses refreshed. Both land here, so the
     * panel is simply redrawn from whatever the backend last said rather than
     * being patched node by node.
     *
     * Node shape (verified against a live run): id, agent_id, description,
     * dependencies, produces, consumes, rationale, status, routed_from.
     */
    /**
     * Draw the plan as a graph.
     *
     * It was a flat list, which threw away the one thing a DAG is for: a step
     * with two parents and a step that merely follows another looked identical.
     * Dependencies are drawn as real edges now, laid out in ranks so the shape
     * of the plan -- what forks, what joins, what waits -- is visible at a
     * glance.
     */
    renderPlan(nodes, source) {
        if (!this.planCard) return;
        if (!Array.isArray(nodes) || !nodes.length) {
            this.planCard.style.display = 'none';
            this.planNodes = [];
            return;
        }
        this.planCard.style.display = '';
        // Remembered as well as displayed: a badge redraw has no `source` of
        // its own, and re-rendering would otherwise reset the label to
        // "planner" on a plan the model actually wrote.
        if (source) this.planSourceValue = source;
        if (this.planSource) this.planSource.textContent = this.planSourceValue || 'planner';
        this.planNodes = nodes;

        this.drawDag(document.getElementById('dagSvg'), nodes, 300);
        const overlay = document.getElementById('dagOverlay');
        if (overlay && overlay.style.display !== 'none') {
            this.drawDag(document.getElementById('dagSvgFull'), nodes, 0);
        }
        this.renderDagLegend(nodes);
    }

    /**
     * Rank the steps by dependency depth.
     *
     * Depth is the LONGEST path from a root, not the shortest: a step that
     * depends on both s1 and s3 has to sit below s3, or its edge would point
     * upwards and the graph would read as a cycle.
     */
    layoutDag(nodes) {
        const byId = new Map();
        const list = nodes.map((n, i) => {
            const id = n.id || n.step_id || `s${i + 1}`;
            const rec = { raw: n, id, deps: (n.dependencies || []).filter(Boolean), depth: 0, i };
            byId.set(id, rec);
            return rec;
        });

        // Iterate to a fixed point. A DAG settles in at most |V| passes; the
        // bound also stops a malformed plan with a cycle from spinning here.
        for (let pass = 0; pass < list.length; pass++) {
            let moved = false;
            for (const nd of list) {
                for (const d of nd.deps) {
                    const p = byId.get(d);
                    if (p && p.depth + 1 > nd.depth) { nd.depth = p.depth + 1; moved = true; }
                }
            }
            if (!moved) break;
        }

        const ranks = [];
        for (const nd of list) (ranks[nd.depth] || (ranks[nd.depth] = [])).push(nd);
        return { list, byId, ranks };
    }

    drawDag(svg, nodes, fixedHeight) {
        if (!svg) return;
        const { list, byId, ranks } = this.layoutDag(nodes);

        // Width comes from the viewport the svg sits in. Falls back rather than
        // throwing: a hidden panel measures 0, and the overlay is measured
        // before it has been laid out at least once.
        const host = svg.parentElement;
        const box = host && host.getBoundingClientRect ? host.getBoundingClientRect() : null;
        const W = Math.max(240, (box && box.width) || 480);
        const rankH = 92;
        const H = fixedHeight || Math.max(240, ranks.length * rankH + 40);
        const R = 15;

        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', H);

        // Position: ranks down the page, evenly spread across each row.
        const usableH = H - 48;
        ranks.forEach((row, d) => {
            const y = 30 + (ranks.length === 1 ? usableH / 2 : (d / (ranks.length - 1)) * usableH);
            row.forEach((nd, i) => {
                nd.x = ((i + 1) / (row.length + 1)) * W;
                nd.y = y;
            });
        });

        const STATUS = {
            completed: 'var(--green)',
            running:   'var(--amber-bright)',
            active:    'var(--amber-bright)',
            failed:    '#e2603f',
            pending:   'var(--muted)',
        };
        const statusOf = n => (n.status || 'pending').toLowerCase();

        let edges = '';
        let dots  = '';

        for (const nd of list) {
            for (const d of nd.deps) {
                const p = byId.get(d);
                if (!p) continue;
                // Cubic bezier with vertical handles -- the Obsidian look, and
                // it keeps edges from cutting straight through nodes between.
                const dy = Math.max(24, (nd.y - p.y) * 0.45);
                edges += `<path class="dag-edge" data-from="${p.id}" data-to="${nd.id}"
                    d="M ${p.x} ${p.y + R} C ${p.x} ${p.y + R + dy}, ${nd.x} ${nd.y - R - dy}, ${nd.x} ${nd.y - R}" />`;
            }
        }

        for (const nd of list) {
            const n = nd.raw;
            const st = statusOf(n);
            const statusColour = STATUS[st] || STATUS.pending;
            const meeting = n.kind === 'meeting';
            const who = n.agent_id || n.agent || '';
            const agent = this.agents.get(who);
            const name = agent ? agent.name : who;
            // Fill says WHO, ring says WHERE IT GOT TO. Status alone left five
            // identical amber dots in a wave and no way to see that three
            // different specialists were working; the manifest colour is the
            // same one the dock card and the name plate already use, so a node
            // and its desk read as the same agent.
            //
            // A step nobody has started yet has no owner worth colouring in,
            // and drawing it in the agent's colour makes a plan look further
            // along than it is -- pending stays muted.
            const colour = (st === 'pending' || !agent || !agent.color)
                ? statusColour : agent.color;
            const guests = (n.attendees || []).map(g => (this.agents.get(g) || {}).name || g);
            const label = meeting ? 'Meeting' : (nd.id);
            const sub = meeting
                ? `${name}${guests.length ? ' + ' + guests.join(', ') : ''}`
                : name;
            const glyph = meeting ? '☰'
                : ({ completed: '✔', running: '▶', active: '▶',
                     failed: '✖' }[st] || '·');

            const badge = this.stepBadges.get(nd.id) || {};
            const spent = [
                Number.isFinite(n.duration_sec) ? `${n.duration_sec}s` : '',
                Number.isFinite(n.cost_usd) && n.cost_usd > 0
                    ? `$${n.cost_usd.toFixed(4)}${n.estimated ? ' ~est' : ''}` : '',
                Number.isFinite(n.total_tokens) && n.total_tokens
                    ? `${n.total_tokens} tok` : '',
            ].filter(Boolean).join(' · ');

            const tip = [
                `${nd.id}${meeting ? ' (meeting)' : ''} — ${sub}`,
                n.description || '',
                n.produces ? `produces: ${n.produces}` : '',
                (n.consumes && n.consumes.length) ? `consumes: ${[].concat(n.consumes).join(', ')}` : '',
                nd.deps.length ? `after: ${nd.deps.join(', ')}` : '',
                spent,
                badge.routed ? `rerouted: ${badge.routed.from} → ${badge.routed.to}` : '',
                badge.critique ? `reviewed: ${badge.critique.verdict}` +
                    (badge.critique.score === null ? '' : ` (${Math.round(badge.critique.score * 100)}%)`) +
                    (badge.critique.reason ? ` — ${badge.critique.reason}` : '') : '',
            ].filter(Boolean).join('\n');

            // Badges ride on the node's shoulders: reroute on the left,
            // review verdict on the right, so the two never collide and the
            // eye can find "this step changed hands" without reading a tooltip.
            let badges = '';
            if (badge.routed) {
                badges += `
                <g class="dag-badge dag-badge-route">
                  <circle cx="${nd.x - R - 2}" cy="${nd.y - R - 2}" r="7" />
                  <text x="${nd.x - R - 2}" y="${nd.y - R - 2}"
                        text-anchor="middle" dominant-baseline="central">⇄</text>
                </g>`;
            }
            if (badge.critique) {
                const ok = badge.critique.verdict === 'accept';
                const pct = badge.critique.score === null || badge.critique.score === undefined
                    ? '?' : String(Math.round(badge.critique.score * 100));
                badges += `
                <g class="dag-badge dag-badge-critique" data-verdict="${ok ? 'accept' : 'revise'}"
                   data-repairing="${badge.critique.repairing ? 'true' : 'false'}">
                  <circle cx="${nd.x + R + 2}" cy="${nd.y - R - 2}" r="8" />
                  <text x="${nd.x + R + 2}" y="${nd.y - R - 2}"
                        text-anchor="middle" dominant-baseline="central">${pct}</text>
                </g>`;
            }

            dots += `
              <g class="dag-node" data-id="${nd.id}" data-status="${st}"${meeting ? ' data-kind="meeting"' : ''}>
                <title>${this.escapeHtml(tip)}</title>
                ${meeting ? `<circle class="dag-ring" cx="${nd.x}" cy="${nd.y}" r="${R + 5}"
                     fill="none" stroke="${colour}" />` : ''}
                <circle class="dag-dot" cx="${nd.x}" cy="${nd.y}" r="${R}"
                        fill="${colour}" stroke="${statusColour}" />
                <text class="dag-glyph" x="${nd.x}" y="${nd.y}"
                      text-anchor="middle" dominant-baseline="central">${glyph}</text>
                ${badges}
                <text class="dag-label" x="${nd.x}" y="${nd.y + R + 15}"
                      text-anchor="middle">${this.escapeHtml(label)}</text>
                <text class="dag-sub" x="${nd.x}" y="${nd.y + R + 27}"
                      text-anchor="middle">${this.escapeHtml(this.clip(sub, 18))}</text>
                ${spent ? `<text class="dag-spent" x="${nd.x}" y="${nd.y + R + 38}"
                      text-anchor="middle">${this.escapeHtml(spent)}</text>` : ''}
              </g>`;
        }

        svg.innerHTML = `<g class="dag-edges">${edges}</g><g class="dag-nodes">${dots}</g>`;
        this.wireDagHover(svg);
    }

    /** Obsidian's hover: the node and its links stay lit, everything else dims. */
    wireDagHover(svg) {
        const nodes = svg.querySelectorAll('.dag-node');
        const edges = svg.querySelectorAll('.dag-edge');
        nodes.forEach(g => {
            g.addEventListener('mouseenter', () => {
                const id = g.dataset.id;
                const near = new Set([id]);
                edges.forEach(e => {
                    const hit = e.dataset.from === id || e.dataset.to === id;
                    e.classList.toggle('lit', hit);
                    e.classList.toggle('dim', !hit);
                    if (hit) { near.add(e.dataset.from); near.add(e.dataset.to); }
                });
                nodes.forEach(o => o.classList.toggle('dim', !near.has(o.dataset.id)));
            });
            g.addEventListener('mouseleave', () => {
                edges.forEach(e => e.classList.remove('lit', 'dim'));
                nodes.forEach(o => o.classList.remove('dim'));
            });
        });
    }

    renderDagLegend(nodes) {
        const el = document.getElementById('dagLegend');
        if (!el) return;
        const tally = { completed: 0, running: 0, failed: 0, pending: 0 };
        nodes.forEach(n => {
            let s = (n.status || 'pending').toLowerCase();
            if (s === 'active') s = 'running';
            if (tally[s] === undefined) s = 'pending';
            tally[s]++;
        });
        const bits = [];
        if (tally.completed) bits.push(`<span data-k="completed">✔ ${tally.completed} done</span>`);
        if (tally.running)   bits.push(`<span data-k="running">▶ ${tally.running} running</span>`);
        if (tally.failed)    bits.push(`<span data-k="failed">✖ ${tally.failed} failed</span>`);
        if (tally.pending)   bits.push(`<span data-k="pending">· ${tally.pending} pending</span>`);
        el.innerHTML = bits.join('');
    }

    clip(s, n) {
        s = String(s || '');
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    /* --- per-step badges -------------------------------------------------- */

    /**
     * Remember a mark against a step and redraw.
     *
     * `dag.update` restates the whole graph on every status change, so these
     * have to live outside the SVG or the next redraw erases them.
     */
    markStep(stepId, patch) {
        if (!stepId) return;
        const current = this.stepBadges.get(stepId) || {};
        this.stepBadges.set(stepId, Object.assign(current, patch));
        if (this.planNodes && this.planNodes.length) {
            this.renderPlan(this.planNodes, this.planSourceValue);
        }
    }

    /* --- cost and telemetry ----------------------------------------------- */

    /**
     * Start a mission's meter.
     *
     * Called on plan.created. Everything is per-mission: the agents' own token
     * counters are lifetime totals that never reset, so a panel built on those
     * would report this run as the sum of every run the process has served.
     */
    startMissionMeter() {
        this.stepBadges.clear();
        this.cost = {
            startedAt: Date.now(), costUsd: 0, tokens: 0, estimated: false,
            steps: [], byAgent: new Map(), running: true,
        };
        if (this.costCard) this.costCard.style.display = '';
        if (this._costTimer) clearInterval(this._costTimer);
        // Only the clock ticks on its own; everything else is event-driven.
        this._costTimer = setInterval(() => this.renderElapsed(), 1000);
        this.renderCost();
    }

    stopMissionMeter() {
        this.cost.running = false;
        if (this._costTimer) { clearInterval(this._costTimer); this._costTimer = null; }
        this.renderCost();
    }

    /** Fold one dag.update into the meter. */
    updateCost(payload = {}) {
        const nodes = payload.dag || [];
        if (!this.cost.startedAt) this.startMissionMeter();

        this.cost.costUsd = Number(payload.cost_usd || 0);
        this.cost.tokens = Number(payload.mission_tokens || 0);
        this.cost.estimated = Boolean(payload.tokens_estimated);

        this.cost.steps = nodes.map(n => ({
            id: n.id,
            agent: n.agent_id || '',
            status: (n.status || 'pending').toLowerCase(),
            seconds: Number(n.duration_sec || 0),
            cost: Number(n.cost_usd || 0),
            tokens: Number(n.total_tokens || 0),
        }));

        const byAgent = new Map();
        for (const step of this.cost.steps) {
            if (!step.tokens && !step.cost) continue;
            const prior = byAgent.get(step.agent) || { tokens: 0, cost: 0 };
            prior.tokens += step.tokens;
            prior.cost += step.cost;
            byAgent.set(step.agent, prior);
        }
        this.cost.byAgent = byAgent;
        this.renderCost();
    }

    renderElapsed() {
        const el = document.getElementById('costElapsed');
        if (!el || !this.cost.startedAt) return;
        const secs = Math.max(0, Math.round((Date.now() - this.cost.startedAt) / 1000));
        el.textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
    }

    renderCost() {
        if (!this.costCard) return;
        const { costUsd, tokens, estimated, steps, byAgent } = this.cost;

        const total = document.getElementById('costTotal');
        if (total) {
            // The tilde is not decoration. On the no-key path the numbers come
            // from the simulator, and a gauge that prints a guess in the same
            // type as a reading is worse than one that prints nothing.
            total.textContent = `$${costUsd.toFixed(4)}${estimated ? ' ~est' : ''}`;
            total.dataset.estimated = String(estimated);
        }
        const tok = document.getElementById('costTokens');
        if (tok) tok.textContent = `${tokens.toLocaleString()}${estimated ? ' ~' : ''}`;

        const done = steps.filter(s => s.status === 'completed').length;
        const failed = steps.filter(s => s.status === 'failed').length;
        const stepsEl = document.getElementById('costSteps');
        if (stepsEl) {
            stepsEl.textContent = `${done}/${steps.length}` + (failed ? ` (${failed} failed)` : '');
            stepsEl.dataset.failed = String(Boolean(failed));
        }
        this.renderElapsed();

        // Latency strip: one bar per step, height proportional to the slowest.
        const strip = document.getElementById('costLatency');
        if (strip) {
            const slowest = Math.max(0.001, ...steps.map(s => s.seconds));
            strip.innerHTML = steps.map(s => {
                const pct = Math.round((s.seconds / slowest) * 100);
                const agent = this.agents.get(s.agent);
                const colour = (s.status === 'pending' || !agent || !agent.color)
                    ? 'var(--frame-dim)' : agent.color;
                const tip = `${s.id} — ${agent ? agent.name : s.agent}: ` +
                            `${s.seconds ? s.seconds + 's' : 'not started'}` +
                            (s.cost ? `, $${s.cost.toFixed(4)}` : '');
                return `<span class="cost-bar" data-status="${s.status}"
                              title="${this.escapeHtml(tip)}"
                              style="height:${Math.max(4, pct)}%;background:${colour}"></span>`;
            }).join('');
        }

        // Tokens by agent, widest first -- who actually spent the mission.
        const rows = document.getElementById('costAgents');
        if (rows) {
            const entries = [...byAgent.entries()].sort((a, b) => b[1].tokens - a[1].tokens);
            const most = Math.max(1, ...entries.map(([, v]) => v.tokens));
            rows.innerHTML = entries.map(([id, v]) => {
                const agent = this.agents.get(id);
                const colour = (agent && agent.color) || 'var(--amber)';
                return `
                  <div class="cost-agent">
                    <span class="cost-agent-name">${this.escapeHtml(agent ? agent.name : id)}</span>
                    <span class="cost-agent-track">
                      <span class="cost-agent-fill"
                            style="width:${Math.round((v.tokens / most) * 100)}%;background:${colour}"></span>
                    </span>
                    <span class="cost-agent-num">${v.tokens.toLocaleString()}</span>
                  </div>`;
            }).join('') || '<div class="cost-empty">No step has reported yet.</div>';
        }
    }

    /** Kept for app.js compatibility -- highlights the dock card. */
    updateActiveDagNode(specialistId) {
        if (specialistId) this.setAgentState(specialistId, 'active');
    }

    /* --- terminal -------------------------------------------------------- */

    addTerminalLog(message, type = 'INFO') {
        if (!this.terminalLogs) return;

        const timeStr = new Date().toTimeString().slice(0, 8);
        const line = document.createElement('div');
        line.className = 'crt-log-line';

        let tagClass = 'tag-analysis';
        if (type === 'SUCCESS') tagClass = 'tag-success';
        if (type === 'ERROR') tagClass = 'tag-error';

        const body = this.highlight(message);
        line.innerHTML =
            `<span class="log-time">[${timeStr}]</span> ${body} ` +
            `<span class="log-tag ${tagClass}">(${this.escapeHtml(type)})</span>`;

        this.terminalLogs.appendChild(line);

        // Cap the buffer so a long session cannot grow the DOM without bound.
        while (this.terminalLogs.children.length > 300) {
            this.terminalLogs.removeChild(this.terminalLogs.firstChild);
        }
        this.terminalLogs.scrollTop = this.terminalLogs.scrollHeight;
    }

    /** Escape first, then tint quoted strings — matches the reference's log styling. */
    highlight(message) {
        return this.escapeHtml(String(message))
            .replace(/&#39;[^&]*?&#39;/g, m => `<span class="log-string">${m}</span>`);
    }

    /* --- token gauge ----------------------------------------------------- */

    updateTokens(tokenData = {}) {
        const total = tokenData.total_tokens ?? this.currentTokens;
        const max = tokenData.max_tokens || this.maxTokens;
        this.currentTokens = total;
        this.maxTokens = max;

        if (this.tokenUsageValue) {
            this.tokenUsageValue.textContent = `${total}/${max}`;
        }
        if (this.tokenFuelGauge) {
            const ratio = max > 0 ? Math.max(0, Math.min(1, total / max)) : 0;
            const lit = Math.round(ratio * GAUGE_SEGMENTS);
            this.tokenFuelGauge.querySelectorAll('.eq-seg').forEach((seg, i) => {
                seg.dataset.on = String(i < lit);
            });
            this.tokenFuelGauge.setAttribute('aria-valuenow', String(total));
            this.tokenFuelGauge.setAttribute('aria-valuemax', String(max));
        }
    }

    /* --- memo ------------------------------------------------------------ */

    /** Remember what was asked, so the output can be shown against it. */
    setMissionGoal(goal) {
        this.missionGoal = goal || '';
    }

    /**
     * Show the finished mission: the goal, the report, and the real files.
     *
     * `payload` is the whole task.completed body. The second argument is
     * optional so the old one-argument call still works.
     */
    showExecutiveMemo(markdownText, payload) {
        if (!this.executiveMemoCard || !this.memoBodyContent) return;
        payload = payload || {};
        if (payload.goal) {
            this.missionGoal = payload.goal;
        }

        const goalEl = document.getElementById('outGoal');
        if (goalEl) {
            goalEl.innerHTML = this.missionGoal
                ? `<span class="out-goal-tag">ASKED</span> ${this.escapeHtml(this.missionGoal)}`
                : '';
            goalEl.style.display = this.missionGoal ? '' : 'none';
        }

        // A failed run used to render its verdict as if it were a result. Say
        // so instead -- an empty report with no explanation reads like a bug in
        // the UI rather than a mission that did not produce anything.
        const files = Array.isArray(payload.artifacts) ? payload.artifacts : [];
        this.missionDir = payload.mission_dir || '';
        const banner = document.getElementById('outBanner');
        const failed = /\b(\d+)\s+failed\b/i.exec(String(markdownText || ''));
        const nFailed = failed ? parseInt(failed[1], 10) : 0;
        if (banner) {
            if (nFailed > 0) {
                banner.textContent = `${nFailed} step${nFailed === 1 ? '' : 's'} failed — `
                    + `the report below is incomplete. See the terminal for the cause.`;
                banner.dataset.kind = 'error';
                banner.style.display = '';
            } else if (!files.length) {
                banner.textContent = 'No files were produced by this mission.';
                banner.dataset.kind = 'warn';
                banner.style.display = '';
            } else {
                banner.style.display = 'none';
            }
        }

        this.memoBodyContent.innerHTML = (typeof marked !== 'undefined')
            ? marked.parse(String(markdownText || ''))
            : this.escapeHtml(String(markdownText || ''));

        this.renderOutputFiles(files);
        this.liveArtifacts = null;   // the full bodies supersede the running view
        this.executiveMemoCard.style.display = 'flex';
        this.selectOutputTab('report');
    }

    renderOutputFiles(files) {
        const wrap  = document.getElementById('outFiles');
        const count = document.getElementById('outFileCount');
        if (count) count.textContent = String(files.length);
        if (!wrap) return;

        // Where the run was saved. The output used to exist only in this tab,
        // so saying where it landed is the point of having persisted it.
        const where = this.missionDir
            ? `<div class="out-where">Saved to <code>${this.escapeHtml(this.missionDir)}</code></div>`
            : '';

        if (!files.length) {
            wrap.innerHTML = where +
                '<div class="out-empty">Nothing was filed to the workspace.</div>';
            return;
        }

        wrap.innerHTML = where + files.map((f, i) => {
            const body = String(f.content || '');
            const rendered = (typeof marked !== 'undefined' && /\.md$/i.test(f.name || ''))
                ? marked.parse(body)
                : `<pre>${this.escapeHtml(body)}</pre>`;
            return `
              <details class="out-file"${i === 0 ? ' open' : ''}>
                <summary>
                    <span class="out-file-name">${this.escapeHtml(f.name || 'untitled')}</span>
                    <span class="out-file-meta">${this.escapeHtml(f.author_name || '')} · ${f.size_chars || 0} chars</span>
                </summary>
                <div class="out-file-body markdown-body">${rendered}${
                    f.truncated ? '<p class="out-clip">… truncated for display</p>' : ''
                }</div>
              </details>`;
        }).join('');
    }

    /**
     * Note an artifact the moment a desk produces it.
     *
     * The output panel used to stay hidden until `task.completed`, so a
     * five-minute mission showed a scrolling terminal and nothing else, then a
     * wall of text at the end. The work exists well before that -- each
     * `artifact.created` carries a name, an author and a 180-character preview
     * -- and showing it as it lands is the difference between watching progress
     * and watching a log.
     *
     * Deliberately additive: `showExecutiveMemo` still replaces this with the
     * full bodies when the mission finishes. This is the running view, not a
     * second source of truth.
     */
    noteArtifact(payload) {
        if (!this.executiveMemoCard) return;
        const name = payload.name || 'untitled';

        this.liveArtifacts = this.liveArtifacts || new Map();
        this.liveArtifacts.set(name, {
            name,
            author_name: payload.author_name || payload.agent_name || '',
            size_chars: payload.size_chars || 0,
            preview: payload.preview || '',
        });

        // Opened on the first artifact, and on the FILES tab, because at this
        // point there is no report to show -- only work.
        this.executiveMemoCard.style.display = 'flex';
        if (this.liveArtifacts.size === 1) this.selectOutputTab('files');

        const goalEl = document.getElementById('outGoal');
        if (goalEl && this.missionGoal) {
            goalEl.innerHTML =
                `<span class="out-goal-tag">ASKED</span> ${this.escapeHtml(this.missionGoal)}`;
            goalEl.style.display = '';
        }

        const count = document.getElementById('outFileCount');
        if (count) count.textContent = String(this.liveArtifacts.size);

        const wrap = document.getElementById('outFiles');
        if (!wrap) return;
        const items = [...this.liveArtifacts.values()];
        wrap.innerHTML =
            '<div class="out-where out-live">Working — files appear here as each ' +
            'desk finishes. The full text lands when the mission ends.</div>' +
            items.map((f, i) => `
              <details class="out-file"${i === items.length - 1 ? ' open' : ''}>
                <summary>
                    <span class="out-file-name">${this.escapeHtml(f.name)}</span>
                    <span class="out-file-meta">${this.escapeHtml(f.author_name)} · ${f.size_chars} chars</span>
                </summary>
                <div class="out-file-body markdown-body"><pre>${
                    this.escapeHtml(f.preview)}${f.preview ? ' …' : ''}</pre></div>
              </details>`).join('');
    }

    /**
     * Move the output card between the sidebar and a full-screen overlay.
     *
     * `want` forces a state; omitted, it toggles. The card's original position
     * is remembered as a parent plus next-sibling pair, so it goes back exactly
     * where it was rather than being appended to the end of the sidebar.
     */
    toggleOutputExpanded(want) {
        const card = this.executiveMemoCard;
        const overlay = document.getElementById('outOverlay');
        const host = document.getElementById('outOverlayHost');
        const btn = document.getElementById('outExpandBtn');
        if (!card || !overlay || !host) return;

        const expanded = overlay.style.display !== 'none';
        const next = (want === undefined) ? !expanded : want;
        if (next === expanded) return;

        if (next) {
            this._outHome = { parent: card.parentNode, before: card.nextSibling };
            host.appendChild(card);
            overlay.style.display = 'flex';
            card.dataset.expanded = 'true';
            // NOT a cross. The card carries its own close button, and turning
            // this one into a second ✕ put two identical crosses side by side
            // with different meanings -- one restores the panel, one dismisses
            // it. A shrink glyph says which is which.
            if (btn) { btn.textContent = '⤡'; btn.title = 'Restore output to the sidebar'; }
        } else {
            const home = this._outHome;
            if (home && home.parent) home.parent.insertBefore(card, home.before);
            overlay.style.display = 'none';
            delete card.dataset.expanded;
            if (btn) { btn.textContent = '⛶'; btn.title = 'Expand output'; }
        }
    }

    selectOutputTab(which) {
        const tabs = { report: 'outTabReport', files: 'outTabFiles' };
        const panes = { report: 'outPaneReport', files: 'outPaneFiles' };
        Object.keys(tabs).forEach(k => {
            const t = document.getElementById(tabs[k]);
            const p = document.getElementById(panes[k]);
            const on = k === which;
            if (t) { t.dataset.active = String(on); t.setAttribute('aria-selected', String(on)); }
            if (p) p.style.display = on ? '' : 'none';
        });
    }

    downloadReportAsPdf() {
        const goal = this.missionGoal || 'AgentHQ Mission Deliverable';
        const reportEl = this.memoBodyContent;
        if (!reportEl || !reportEl.innerHTML.trim()) {
            this.addTerminalLog('No report available to export as PDF.', 'WARN');
            return;
        }

        this.addTerminalLog('Generating PDF deliverable...', 'INFO');

        const slug = goal
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '')
            .slice(0, 40) || 'agenthq_report';
        const filename = `${slug}.pdf`;

        const activeTab = document.getElementById('outTabFiles')?.dataset.active === 'true' ? 'files' : 'report';

        const container = document.createElement('div');
        container.style.padding = '24px 28px';
        container.style.color = '#0f172a';
        container.style.background = '#ffffff';
        container.style.fontFamily = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";
        container.style.lineHeight = '1.6';
        container.style.fontSize = '13px';

        const now = new Date().toLocaleString(undefined, { 
            dateStyle: 'medium', 
            timeStyle: 'short' 
        });

        let bodyHtml = '';
        if (activeTab === 'files') {
            const outFiles = document.getElementById('outFiles');
            if (outFiles && outFiles.children.length) {
                bodyHtml = `
                    <h2 style="font-size: 16px; color: #1e3a8a; border-bottom: 1.5px solid #cbd5e1; padding-bottom: 6px; margin-top: 18px;">Mission Deliverable Files</h2>
                    ${Array.from(outFiles.querySelectorAll('.out-file')).map(f => {
                        const name = f.querySelector('.out-file-name')?.innerText || 'File';
                        const meta = f.querySelector('.out-file-meta')?.innerText || '';
                        const content = f.querySelector('.out-file-body')?.innerHTML || '';
                        return `
                            <div style="margin-top: 20px; page-break-inside: avoid;">
                                <div style="font-weight: 700; font-size: 13.5px; color: #1e293b; display: flex; justify-content: space-between; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px;">
                                    <span>📄 ${this.escapeHtml(name)}</span>
                                    <span style="font-size: 11px; color: #64748b;">${this.escapeHtml(meta)}</span>
                                </div>
                                <div style="margin-top: 10px;">${content}</div>
                            </div>
                        `;
                    }).join('')}
                `;
            }
        }

        if (!bodyHtml) {
            bodyHtml = `<div style="margin-top: 10px;">${reportEl.innerHTML}</div>`;
        }

        container.innerHTML = `
            <div style="border-bottom: 2.5px solid #2563eb; padding-bottom: 12px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-end;">
                <div>
                    <div style="font-size: 20px; font-weight: 800; color: #1e3a8a; letter-spacing: -0.5px;">AgentHQ Executive Deliverable</div>
                    <div style="font-size: 12px; color: #64748b; margin-top: 3px;">Autonomous AI Multi-Agent Intelligence Platform</div>
                </div>
                <div style="text-align: right; font-size: 11px; color: #64748b;">
                    <div>${this.escapeHtml(now)}</div>
                    <div style="font-weight: 700; color: #2563eb; margin-top: 2px;">AUTHENTICATED REPORT</div>
                </div>
            </div>
            ${goal ? `
            <div style="background: #f8fafc; border-left: 4px solid #2563eb; padding: 10px 14px; margin-bottom: 20px; border-radius: 0 6px 6px 0;">
                <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px;">Mission Goal</div>
                <div style="font-size: 13.5px; font-weight: 600; color: #0f172a; margin-top: 2px;">${this.escapeHtml(goal)}</div>
            </div>` : ''}
            <div class="pdf-content-body">
                ${bodyHtml}
            </div>
            <div style="margin-top: 36px; padding-top: 12px; border-top: 1px solid #e2e8f0; display: flex; justify-content: space-between; font-size: 10px; color: #94a3b8;">
                <span>Verified AgentHQ Output &middot; Zero-Hallucination Extraction</span>
                <span>Page 1 of 1</span>
            </div>
        `;

        container.querySelectorAll('table').forEach(t => {
            t.style.width = '100%';
            t.style.borderCollapse = 'collapse';
            t.style.marginTop = '14px';
            t.style.marginBottom = '14px';
            t.style.fontSize = '12px';
        });
        container.querySelectorAll('th').forEach(th => {
            th.style.background = '#f1f5f9';
            th.style.border = '1px solid #cbd5e1';
            th.style.padding = '8px 10px';
            th.style.fontWeight = '700';
            th.style.textAlign = 'left';
            th.style.color = '#0f172a';
        });
        container.querySelectorAll('td').forEach(td => {
            td.style.border = '1px solid #e2e8f0';
            td.style.padding = '7px 10px';
            td.style.color = '#334155';
        });
        container.querySelectorAll('pre').forEach(p => {
            p.style.background = '#f8fafc';
            p.style.border = '1px solid #e2e8f0';
            p.style.padding = '10px 12px';
            p.style.borderRadius = '4px';
            p.style.fontSize = '11.5px';
            p.style.whiteSpace = 'pre-wrap';
            p.style.wordBreak = 'break-all';
        });
        container.querySelectorAll('a').forEach(a => {
            a.style.color = '#2563eb';
            a.style.textDecoration = 'none';
        });

        if (typeof html2pdf !== 'undefined') {
            const opt = {
                margin: [10, 10, 10, 10],
                filename: filename,
                image: { type: 'jpeg', quality: 0.98 },
                html2canvas: { scale: 2, useCORS: true, letterRendering: true, logging: false },
                jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
                pagebreak: { mode: ['avoid-all', 'css', 'legacy'] }
            };
            html2pdf().set(opt).from(container).save().then(() => {
                this.addTerminalLog(`Downloaded ${filename} successfully.`, 'SUCCESS');
            }).catch(err => {
                console.error('html2pdf error:', err);
                this.printFallback(container, filename);
            });
        } else {
            this.printFallback(container, filename);
        }
    }

    printFallback(container, filename) {
        const printWin = window.open('', '_blank');
        if (!printWin) {
            this.addTerminalLog('Popup blocked. Please allow popups to download PDF.', 'WARN');
            return;
        }
        printWin.document.write(`<!DOCTYPE html><html><head><title>${this.escapeHtml(filename)}</title><style>@page{margin:15mm;size:A4 portrait;}body{margin:0;padding:0;}</style></head><body>${container.innerHTML}<script>window.onload=function(){window.focus();window.print();setTimeout(function(){window.close();},1000);};<\/script></body></html>`);
        printWin.document.close();
        this.addTerminalLog(`Print dialogue opened for ${filename}.`, 'SUCCESS');
    }

    /* --- wiring ---------------------------------------------------------- */

    initEventListeners() {
        const clearBtn = document.getElementById('clearTerminalBtn');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                this.terminalLogs.innerHTML =
                    '<div class="terminal-prompt-intro">Live terminal log stream.</div>';
                this.addTerminalLog('Console reset.', 'SUCCESS');
            });
        }

        const closeMemo = document.getElementById('closeMemoBtn');
        if (closeMemo) {
            closeMemo.addEventListener('click', () => {
                // Collapse FIRST. The card physically lives inside the overlay
                // while expanded, so hiding it on its own left the blurred
                // backdrop covering the screen with nothing in it -- and the
                // card back in the sidebar, hidden, so a second click on a
                // button the user could no longer see was the only way out.
                this.toggleOutputExpanded(false);
                this.executiveMemoCard.style.display = 'none';
            });
        }

        // Expand / restore the output panel. The card itself moves between the
        // sidebar and the overlay -- rendering a second copy would mean two
        // DOM trees to keep in step, and the open <details>, the selected tab
        // and the scroll position would all reset on every toggle.
        const outExpand = document.getElementById('outExpandBtn');
        if (outExpand) outExpand.addEventListener('click', () => this.toggleOutputExpanded());

        const outPdf = document.getElementById('downloadPdfBtn');
        if (outPdf) outPdf.addEventListener('click', () => this.downloadReportAsPdf());

        const outOverlay = document.getElementById('outOverlay');
        if (outOverlay) {
            outOverlay.addEventListener('click', e => {
                if (e.target === outOverlay) this.toggleOutputExpanded(false);
            });
        }

        const tabReport = document.getElementById('outTabReport');
        const tabFiles  = document.getElementById('outTabFiles');
        if (tabReport) tabReport.addEventListener('click', () => this.selectOutputTab('report'));
        if (tabFiles)  tabFiles.addEventListener('click',  () => this.selectOutputTab('files'));

        // The sidebar graph is a thumbnail; the overlay is where a wide plan is
        // actually readable. Redraw on open because the viewport size differs.
        const overlay = document.getElementById('dagOverlay');
        const expand  = document.getElementById('dagExpandBtn');
        const closeD  = document.getElementById('dagCloseBtn');
        if (expand && overlay) {
            expand.addEventListener('click', () => {
                overlay.style.display = 'flex';
                requestAnimationFrame(() =>
                    this.drawDag(document.getElementById('dagSvgFull'), this.planNodes || [], 0));
            });
        }
        if (closeD && overlay) {
            closeD.addEventListener('click', () => { overlay.style.display = 'none'; });
        }
        if (overlay) {
            overlay.addEventListener('click', e => {
                if (e.target === overlay) overlay.style.display = 'none';
            });
        }
        document.addEventListener('keydown', e => {
            if (e.key !== 'Escape') return;
            if (overlay && overlay.style.display !== 'none') {
                overlay.style.display = 'none';
                return;
            }
            // The output card has to be moved back, not just hidden, so it
            // goes through its own toggle rather than having its display set.
            this.toggleOutputExpanded(false);
        });

    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}
