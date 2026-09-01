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
        this.taskQueueList  = document.getElementById('taskQueueList');
        this.taskQueueCount = document.getElementById('taskQueueCount');
        this.netStatusText  = document.getElementById('netStatusText');
        this.agentDock      = document.getElementById('agentDock');
        this.planCard       = document.getElementById('planCard');
        this.planList       = document.getElementById('planList');
        this.planSource     = document.getElementById('planSource');
        this.simSpeedText   = document.getElementById('simSpeedText');
        this.executiveMemoCard = document.getElementById('executiveMemoCard');
        this.memoBodyContent   = document.getElementById('memoBodyContent');

        this.currentTokens = 0;
        this.maxTokens = 10000;
        this.maxQueue = 5;
        this.queue = [];
        this.agents = new Map();
        this.selectedAgentId = null;

        this.atlas = null;              // { size, frames, characters }

        this.buildGauge();
        this.registerAgents(DEFAULT_AGENTS);
        this.renderQueue();
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

        this.setAgentCount(this.agents.size);
        this.applyAvatars();            // no-op until the atlas has loaded
    }

    spriteFor(id) {
        const found = DEFAULT_AGENTS.find(a => a.id === id);
        return found ? found.sprite : 'Adam';
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
    renderPlan(nodes, source) {
        if (!this.planList || !this.planCard) return;
        if (!Array.isArray(nodes) || !nodes.length) {
            this.planCard.style.display = 'none';
            return;
        }
        this.planCard.style.display = '';
        if (this.planSource) this.planSource.textContent = source || 'planner';

        this.planList.innerHTML = nodes.map((n, i) => {
            const id     = n.id || n.step_id || `s${i + 1}`;
            const status = (n.status || 'pending').toLowerCase();
            const who    = n.agent_id || n.agent || '';
            const text   = n.description || n.label || id;
            const agent  = this.agents.get(who);
            const name   = agent ? agent.name : who;
            const deps   = n.dependencies || [];
            const meeting = n.kind === 'meeting';
            const mark   = meeting ? '☰'
                         : ({ completed: '✔', running: '▶', active: '▶',
                              failed: '✖', pending: '·' }[status] || '·');
            // A meeting is several people in one place, so the row names the
            // room rather than a single desk: chair first, then who joined.
            const guests = (n.attendees || [])
                .map(id => (this.agents.get(id) || {}).name || id);
            const line = meeting
                ? `${name}${guests.length ? ' + ' + guests.join(', ') : ''}`
                : name;
            return `
                <div class="plan-step" data-status="${status}"${meeting ? ' data-kind="meeting"' : ''}>
                    <span class="plan-mark">${mark}</span>
                    <span class="plan-body">
                        <span class="plan-text">${meeting ? 'Meeting — ' : ''}${this.escapeHtml(text)}</span>
                        <span class="plan-meta">${this.escapeHtml(line || '')}${
                            deps.length ? ' · after ' + this.escapeHtml(deps.join(', ')) : ''
                        }</span>
                    </span>
                </div>`;
        }).join('');
    }

    /** Kept for app.js compatibility -- highlights the dock card. */
    updateActiveDagNode(specialistId) {
        if (specialistId) this.setAgentState(specialistId, 'active');
    }

    setAgentCount(n) {
        if (this.netStatusText) {
            this.netStatusText.textContent = `${n} AGENT${n === 1 ? '' : 'S'} ONLINE`;
        }
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

    /* --- task queue ------------------------------------------------------ */

    enqueueTask(id, description) {
        this.queue.push({ id, description, active: this.queue.length === 0 });
        this.renderQueue();
    }

    completeTask(id) {
        this.queue = this.queue.filter(t => t.id !== id);
        if (this.queue.length) this.queue[0].active = true;
        this.renderQueue();
    }

    setQueue(tasks) {
        this.queue = (tasks || []).map((t, i) => ({
            id: t.id,
            description: t.description,
            active: i === 0
        }));
        this.renderQueue();
    }

    renderQueue() {
        if (!this.taskQueueList) return;
        this.taskQueueList.innerHTML = '';

        if (!this.queue.length) {
            const empty = document.createElement('div');
            empty.className = 'queue-empty';
            empty.textContent = 'No tasks queued.';
            this.taskQueueList.appendChild(empty);
        } else {
            this.queue.forEach(task => {
                const row = document.createElement('div');
                row.className = 'queue-row';
                row.dataset.active = String(!!task.active);
                row.textContent = `${task.id} ${task.description}`;
                this.taskQueueList.appendChild(row);
            });
        }

        if (this.taskQueueCount) {
            this.taskQueueCount.textContent = `${this.queue.length}/${this.maxQueue}`;
        }
    }

    /* --- memo ------------------------------------------------------------ */

    showExecutiveMemo(markdownText) {
        if (!this.executiveMemoCard || !this.memoBodyContent) return;
        this.memoBodyContent.innerHTML = (typeof marked !== 'undefined')
            ? marked.parse(String(markdownText || ''))
            : this.escapeHtml(String(markdownText || ''));
        this.executiveMemoCard.style.display = 'flex';
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
                this.executiveMemoCard.style.display = 'none';
            });
        }

        this.wireSimControls();
    }

    wireSimControls() {
        const play = document.getElementById('simPlayBtn');
        const pause = document.getElementById('simPauseBtn');
        const rewind = document.getElementById('simRewindBtn');
        const toggle = document.getElementById('simToggleLabel');

        const canvas = () => window.officeCanvas;
        const setPaused = (paused) => {
            const c = canvas();
            if (c) c.isPaused = paused;
            if (play) play.dataset.active = String(!paused);
            if (pause) pause.dataset.active = String(paused);
            if (toggle) toggle.textContent = paused ? '[Resume]' : '[Pause]';
        };

        if (play) play.addEventListener('click', () => setPaused(false));
        if (pause) pause.addEventListener('click', () => setPaused(true));
        if (toggle) {
            toggle.addEventListener('click', () => {
                const c = canvas();
                setPaused(c ? !c.isPaused : true);
            });
        }

        // Rewind steps the speed back down through the cycle 1x -> 2x -> 4x.
        if (rewind) {
            rewind.addEventListener('click', () => {
                const c = canvas();
                if (!c) return;
                const speeds = [1, 2, 4];
                const idx = speeds.indexOf(c.simSpeed);
                c.simSpeed = speeds[(idx <= 0 ? speeds.length : idx) - 1];
                if (this.simSpeedText) this.simSpeedText.textContent = `Speed ${c.simSpeed}x`;
            });
        }

        setPaused(false);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}
