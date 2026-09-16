/**
 * Trace replay — the office as a time-travel debugger.
 *
 * Every mission leaves a `trace.jsonl` beside its artifacts: one span per
 * mission, step, attempt, turn, tool call, debate turn and model call, each
 * with a real start and end. That file is the run. This drives the office from
 * it, so a mission that finished last week can be watched again — paused,
 * scrubbed, and re-run at 8×.
 *
 * The renderer is not forked and `canvas.js` is not touched. Spans are turned
 * back into the same events the WebSocket sends, and handed to the same
 * `handleEvent` in `app.js` that the live socket feeds. Anything the office can
 * show live, it can show from a trace; anything it cannot, replay cannot invent.
 *
 * Two files make one replay:
 *   * `GET /api/missions/{id}`       — the plan, for the DAG's shape and each
 *                                      step's description and dependencies
 *   * `GET /api/missions/{id}/trace` — the spans, for what happened and when
 *
 * The trace holds no coordinates, so walks are reconstructed: a `tool` span
 * names a tool, `GET /api/office` says which station that tool is bound to, and
 * the agent walks there. That is exactly what happened at the time, recovered
 * rather than invented.
 */

class TraceReplay {
    /** `sink` is the live event handler — the same one the socket calls. */
    constructor(sink) {
        this.sink = sink;
        this.events = [];        // { t, event } — t is seconds from mission start
        this.cursor = 0;         // index of the next event to fire
        this.clock = 0;          // seconds into the trace
        this.duration = 0;
        this.speed = 2;
        this.playing = false;
        this.active = false;     // true from the moment a trace is loaded
        this.missionId = '';
        this.toolStations = new Map();   // tool name -> {x, y, label}
        this._raf = null;
        this._last = 0;

        this.el = {
            card:    document.getElementById('replayCard'),
            select:  document.getElementById('replayMission'),
            refresh: document.getElementById('replayRefreshBtn'),
            play:    document.getElementById('replayPlayBtn'),
            speed:   document.getElementById('replaySpeed'),
            exit:    document.getElementById('replayExitBtn'),
            scrub:   document.getElementById('replayScrub'),
            status:  document.getElementById('replayStatus'),
            clockEl: document.getElementById('replayClock'),
        };
        this.wire();
        this.loadStations();
        this.loadMissions();
    }

    /* --- wiring ---------------------------------------------------------- */

    wire() {
        const { select, refresh, play, speed, exit, scrub } = this.el;
        if (select) select.addEventListener('change', () => this.load(select.value));
        if (refresh) refresh.addEventListener('click', () => this.loadMissions());
        if (play) play.addEventListener('click', () => (this.playing ? this.pause() : this.play()));
        if (speed) speed.addEventListener('change', () => { this.speed = Number(speed.value) || 1; });
        if (exit) exit.addEventListener('click', () => this.exit());
        if (scrub) {
            scrub.addEventListener('input', () => {
                const to = (Number(scrub.value) / 1000) * this.duration;
                this.seek(to);
            });
        }
    }

    status(text) {
        if (this.el.status) this.el.status.textContent = text;
    }

    async loadStations() {
        try {
            const res = await fetch('/api/office');
            if (!res.ok) return;
            const data = await res.json();
            for (const station of data.stations || []) {
                for (const tool of station.tools || []) {
                    this.toolStations.set(tool, {
                        x: station.x, y: station.y, label: station.label, id: station.id,
                    });
                }
            }
        } catch (e) {
            // Not fatal: without station geometry the agents simply do not walk.
            console.warn('replay: could not read the office layout', e);
        }
    }

    async loadMissions(selectedId = null) {
        const select = this.el.select;
        if (!select) return;
        try {
            const res = await fetch('/api/missions?limit=50');
            const data = await res.json();
            const missions = (data.missions || []).filter(m => m.id);
            select.innerHTML = '<option value="">— Select a past chat / report —</option>' +
                missions.map(m => {
                    // `started_at` is epoch seconds from the run store
                    const when = m.started_at
                        ? new Date(m.started_at * 1000).toISOString().replace('T', ' ').slice(0, 16)
                        : '';
                    const goal = (m.goal || m.id).slice(0, 60);
                    const isSelected = selectedId && m.id === selectedId ? ' selected' : '';
                    return `<option value="${m.id}"${isSelected}>${when} · ${goal}</option>`;
                }).join('');
            this.status(missions.length
                ? `${missions.length} past chat(s) on record.`
                : 'No past chats on record yet — run one first.');
        } catch (e) {
            select.innerHTML = '<option value="">— unavailable —</option>';
            this.status('Could not reach the mission history.');
        }
    }

    /* --- loading a trace -------------------------------------------------- */

    /**
     * One artifact's text, or '' if it is not there.
     *
     * Never throws: a missing report is a replay that says so, not a replay
     * that fails to load.
     */
    async fetchArtifact(missionId, name) {
        try {
            const res = await fetch(
                `/api/missions/${missionId}/artifacts/${encodeURIComponent(name)}`);
            return res.ok ? await res.text() : '';
        } catch (e) {
            return '';
        }
    }

    async load(missionId) {
        if (!missionId) return;
        this.stop();
        this.status('Loading past chat & report…');
        let mission = {}, spans = [];
        try {
            const [mRes, tRes] = await Promise.all([
                fetch(`/api/missions/${missionId}`),
                fetch(`/api/missions/${missionId}/trace`),
            ]);
            mission = mRes.ok ? await mRes.json() : {};
            if (tRes && tRes.ok) {
                const text = await tRes.text();
                spans = text.split('\n').filter(Boolean).map(line => {
                    try { return JSON.parse(line); } catch (e) { return null; }
                }).filter(Boolean);
            }
        } catch (e) {
            this.status(`Could not load mission: ${e}`);
            return;
        }

        // If report wasn't returned by the endpoint, fetch fallback
        if (!mission.report) {
            mission.report = await this.fetchArtifact(missionId, 'report.md') ||
                             await this.fetchArtifact(missionId, 'final_report.md');
        }

        // Format artifacts if artifact_details not returned
        if (!mission.artifact_details || !mission.artifact_details.length) {
            const files = Array.isArray(mission.artifacts) ? mission.artifacts : [];
            const details = [];
            for (const f of files) {
                if (f === 'trace.jsonl' || f === 'INDEX.md' || f === 'README.md') continue;
                const content = await this.fetchArtifact(missionId, f);
                if (content) {
                    details.push({
                        name: f,
                        size_chars: content.length,
                        content: content,
                        author_name: 'workspace',
                    });
                }
            }
            mission.artifact_details = details;
        }

        this.missionId = missionId;
        const hud = window.retroHUD;
        const goal = mission.goal || missionId;
        const steps = (mission.plan && mission.plan.steps) || mission.steps || [];
        const files = mission.artifact_details || [];

        // Reset office state
        this.reset();

        // 1. Immediately display report and deliverable files in MISSION OUTPUT
        if (hud) {
            hud.setMissionGoal(goal);
            if (steps.length) {
                hud.renderPlan(steps, (mission.plan && mission.plan.source) || mission.plan_source || 'past');
            }

            // 2. Populate terminal with full past chat & activity logs
            this.renderPastChatLogs(mission, spans);

            const finished = {
                result: mission.report || '_No report markdown recorded for this mission._',
                artifacts: files,
                mission_dir: mission.mission_dir || '',
                // So "download all" addresses the mission being replayed.
                mission_id: mission.id || missionId,
                duration_seconds: mission.duration_seconds || 0,
                steps_completed: steps.filter(s => s.status === 'completed').length,
                steps_failed: steps.filter(s => s.status === 'failed').length,
                replay: true,
                goal: goal,
            };
            hud.showExecutiveMemo(finished.result, finished);
        }

        // 3. Prepare 2D office trace playback if spans are available
        if (spans.length) {
            this.events = this.compile(spans, mission);
            this.duration = this.events.length ? this.events[this.events.length - 1].t : 0;
            this.active = true;
            this.cursor = 0;
            this.clock = 0;

            if (this.el.play) { this.el.play.disabled = false; this.el.play.textContent = '▶ PLAY'; }
            if (this.el.scrub) { this.el.scrub.disabled = false; this.el.scrub.value = '0'; }
            if (this.el.exit) this.el.exit.style.display = '';
            if (this.el.card) this.el.card.dataset.active = 'true';
            if (this.el.clockEl) this.el.clockEl.textContent = `0.0s / ${this.duration.toFixed(1)}s`;

            this.status(`Report & past chat loaded (${spans.length} trace events). Press ▶ PLAY for visual replay.`);
        } else {
            this.active = true;
            this.events = [];
            this.duration = 0;
            if (this.el.play) this.el.play.disabled = true;
            if (this.el.scrub) { this.el.scrub.disabled = true; this.el.scrub.value = '0'; }
            if (this.el.exit) this.el.exit.style.display = '';
            if (this.el.card) this.el.card.dataset.active = 'true';
            if (this.el.clockEl) this.el.clockEl.textContent = '—';
            this.status('Report & past chat loaded. (No 2D movement trace for this run).');
        }
    }

    renderPastChatLogs(mission, spans) {
        const hud = window.retroHUD;
        if (!hud || !hud.terminalLogs) return;

        hud.terminalLogs.innerHTML = '';
        hud.addTerminalLog(`=== PAST CHAT & MISSION LOG: ${mission.id} ===`, 'SUCCESS');
        if (mission.goal) {
            hud.addTerminalLog(`Task: "${mission.goal}"`, 'ANALYSIS');
        }

        const steps = (mission.plan && mission.plan.steps) || mission.steps || [];
        if (steps.length) {
            hud.addTerminalLog(`Plan: ${steps.length} step(s) (${(mission.plan && mission.plan.source) || mission.plan_source || 'planner'})`, 'INFO');
        }

        if (spans && spans.length) {
            const sorted = [...spans].sort((a, b) => (a.t_start - b.t_start) || (a.seq - b.seq));
            for (const span of sorted) {
                const a = span.attrs || {};
                const agentId = a.agent_id || '';
                const agentName = this.agentName(agentId);

                if (span.kind === 'step') {
                    hud.addTerminalLog(`${agentName} started step ${a.step_id || span.name}`, 'ANALYSIS');
                    if (a.verdict) {
                        const score = (a.quality !== undefined && a.quality !== null) ? ` (${Math.round(a.quality * 100)}%)` : '';
                        hud.addTerminalLog(`Step ${a.step_id || span.name} review: ${a.verdict}${score}`, a.verdict === 'accept' ? 'SUCCESS' : 'ANALYSIS');
                    }
                } else if (span.kind === 'debate') {
                    const said = a.position || '';
                    if (said) {
                        hud.addTerminalLog(`${agentName}: "${said}"`, 'INFO');
                    }
                } else if (span.kind === 'tool') {
                    hud.addTerminalLog(`${agentName} executed tool ${span.name}`, span.status === 'error' ? 'ERROR' : 'SUCCESS');
                } else if (span.kind === 'mission') {
                    if (span.status === 'ok') {
                        const dur = span.duration_sec ? ` in ${span.duration_sec.toFixed(1)}s` : '';
                        hud.addTerminalLog(`Mission completed${dur}`, 'SUCCESS');
                    } else if (span.status === 'error') {
                        hud.addTerminalLog(`Mission failed: ${span.error || 'unknown error'}`, 'ERROR');
                    }
                }
            }
        } else {
            for (const step of steps) {
                const name = this.agentName(step.agent_id);
                hud.addTerminalLog(`${name}: ${step.description || step.id} [${step.status || 'done'}]`, 'INFO');
            }
            if (mission.status === 'completed') {
                hud.addTerminalLog(`Mission completed in ${mission.duration_seconds || 0}s`, 'SUCCESS');
            } else if (mission.status === 'unverified') {
                hud.addTerminalLog('Mission finished unverified: a reviewer rejected at least one step', 'WARN');
            }
        }
    }

    /**
     * Spans in, events out.
     *
     * One pass, no interpretation beyond what the span already says: a step
     * span becomes the assignment that opened it and the completion that closed
     * it, a tool span becomes the walk and the two tool events around it, a
     * turn becomes the tokens it burned. Anything the trace does not record —
     * an artifact's text, the words of a chair's summary — is not synthesised,
     * because a replay that fills in blanks is a story, not a record.
     */
    compile(spans, mission) {
        const t0 = Math.min(...spans.map(s => s.t_start));
        const out = [];
        const at = (t, type, agentId, data) =>
            out.push({ t: Math.max(0, t - t0), event: { type, agent_id: agentId, task_id: this.missionId, data } });

        const plan = (mission && mission.plan) || {};
        const steps = plan.steps || mission.steps || [];
        const byStepId = new Map(steps.map(s => [s.id || s.step_id, s]));

        // The plan lands first, so the graph is on screen before anything runs.
        if (steps.length) {
            at(t0, 'plan.created', null, {
                goal: mission.goal || '',
                source: plan.source || mission.plan_source || 'replay',
                steps: steps.map(s => Object.assign({}, s, { status: 'pending' })),
            });
        }
        at(t0, 'task.created', null, { description: mission.goal || this.missionId });

        // Statuses are accumulated as the replay walks forward, so every
        // dag.update carries the whole graph exactly as the live one does.
        const status = new Map(steps.map(s => [s.id || s.step_id, 'pending']));
        const spend = new Map();
        const dagAt = (t) => {
            at(t, 'dag.update', null, {
                source: plan.source || mission.plan_source || 'replay',
                dag: steps.map(s => {
                    const id = s.id || s.step_id;
                    const row = spend.get(id) || {};
                    return Object.assign({}, s, {
                        status: status.get(id) || 'pending',
                        cost_usd: row.cost || 0,
                        duration_sec: row.seconds || 0,
                        total_tokens: row.tokens || 0,
                        estimated: Boolean(row.estimated),
                    });
                }),
                cost_usd: [...spend.values()].reduce((a, r) => a + (r.cost || 0), 0),
                mission_tokens: [...spend.values()].reduce((a, r) => a + (r.tokens || 0), 0),
                tokens_estimated: [...spend.values()].some(r => r.estimated),
            });
        };

        const agentTotals = new Map();
        let finished = null;      // the task.completed payload, tallied at the end
        const sorted = [...spans].sort((a, b) => (a.t_start - b.t_start) || (a.seq - b.seq));

        for (const span of sorted) {
            const a = span.attrs || {};
            const agentId = a.agent_id || '';
            const end = span.t_end || span.t_start;

            switch (span.kind) {
                case 'step': {
                    const stepId = a.step_id || span.name.split(':')[0];
                    const step = byStepId.get(stepId) || {};
                    at(span.t_start, 'task.assigned', agentId, {
                        assigned_to: agentId,
                        agent_name: this.agentName(agentId),
                        subtask_description: step.description || span.name,
                        step_id: stepId,
                        produces: step.produces || '',
                    });
                    status.set(stepId, 'running');
                    dagAt(span.t_start);

                    spend.set(stepId, {
                        cost: Number(a.cost_usd || 0),
                        seconds: Number(span.duration_sec || 0),
                        tokens: Number(a.total_tokens || 0),
                        estimated: Boolean(a.estimated),
                    });
                    const failed = span.status === 'error';
                    status.set(stepId, failed ? 'failed' : 'completed');
                    if (failed) {
                        at(end, 'task.failed', agentId,
                           { step_id: stepId, error: span.error || 'step failed' });
                    }
                    if (a.verdict) {
                        at(end, 'step.critiqued', agentId, {
                            step_id: stepId, verdict: a.verdict,
                            score: (a.quality === undefined ? null : a.quality),
                            reasons: [], source: 'critic', repairing: false,
                            attempt: a.attempts || 1,
                        });
                    }
                    dagAt(end);
                    break;
                }

                case 'tool': {
                    const station = this.toolStations.get(span.name);
                    if (station) {
                        at(span.t_start, 'agent.move', agentId, {
                            agent_name: this.agentName(agentId),
                            to: { x: station.x, y: station.y },
                            label: station.label, station_id: station.id,
                            duration_ms: 500,
                        });
                    }
                    at(span.t_start, 'tool.called', agentId,
                       { tool: span.name, arguments: {} });
                    at(end, 'tool.result', agentId, {
                        tool: span.name,
                        duration_ms: Math.round((span.duration_sec || 0) * 1000),
                        result: { success: span.status !== 'error' },
                    });
                    break;
                }

                case 'turn': {
                    const total = Number(a.prompt_tokens || 0) + Number(a.completion_tokens || 0);
                    if (!total) break;
                    const prior = agentTotals.get(agentId) || 0;
                    agentTotals.set(agentId, prior + total);
                    at(end, 'token.update', agentId, {
                        total_tokens: [...agentTotals.values()].reduce((x, y) => x + y, 0),
                        model: a.model || '',
                        estimated: Boolean(a.estimated),
                    });
                    break;
                }

                case 'debate': {
                    at(span.t_start, 'agent.argued', agentId, {
                        agent_name: this.agentName(agentId),
                        position: a.position || '(the trace recorded the turn, not its words)',
                        round: a.round || 1,
                        step_id: a.step_id || '',
                        simulated: Boolean(a.simulated),
                        model: a.model || '',
                    });
                    break;
                }

                case 'mission': {
                    // The counts are filled in after the loop: the mission span
                    // is the FIRST one in start order, so at this point no step
                    // has been walked yet and every tally would read zero.
                    finished = {
                        result: (mission && mission.report) ||
                            '_This mission left no `report.md` on disk, so the '
                            + 'replay has no briefing to show. The steps and '
                            + 'their costs below are from the trace._',
                        duration_seconds: Number(span.duration_sec || 0).toFixed(2),
                        steps_completed: 0, steps_failed: 0, replay: true,
                        artifacts: (mission && mission.artifact_details) || [],
                        mission_dir: (mission && mission.mission_dir) || '',
                        goal: (mission && mission.goal) || '',
                    };
                    at(end, 'task.completed', null, finished);
                    break;
                }
                default:
                    break;      // attempt, llm, critic: covered by their parents
            }
        }

        if (finished) {
            finished.steps_completed = [...status.values()].filter(v => v === 'completed').length;
            finished.steps_failed = [...status.values()].filter(v => v === 'failed').length;
        }
        return out.sort((x, y) => x.t - y.t);
    }

    agentName(agentId) {
        const hud = window.retroHUD;
        const agent = hud && hud.agents ? hud.agents.get(agentId) : null;
        return agent ? agent.name : agentId;
    }

    /* --- transport -------------------------------------------------------- */

    play() {
        if (!this.active || this.playing) return;
        if (this.clock >= this.duration) this.seek(0);
        this.playing = true;
        this._last = performance.now();
        if (this.el.play) this.el.play.textContent = '❚❚ PAUSE';
        const step = (now) => {
            if (!this.playing) return;
            const dt = ((now - this._last) / 1000) * this.speed;
            this._last = now;
            this.clock = Math.min(this.duration, this.clock + dt);
            this.tick();
            if (this.clock >= this.duration) { this.pause(); return; }
            this._raf = requestAnimationFrame(step);
        };
        this._raf = requestAnimationFrame(step);
    }

    pause() {
        this.playing = false;
        if (this._raf) cancelAnimationFrame(this._raf);
        this._raf = null;
        if (this.el.play) this.el.play.textContent = '▶ PLAY';
    }

    /**
     * Jump to a point in the trace.
     *
     * Backwards means starting over: the office is a state machine driven by
     * events, and there is no inverse for "the agent walked to the library".
     * Replaying from zero to the target is both simpler and exactly correct,
     * and it is fast because the catch-up fires events without waiting.
     */
    seek(seconds) {
        const target = Math.max(0, Math.min(this.duration, seconds));
        if (target < this.clock) { this.reset(); this.cursor = 0; }
        this.clock = target;
        this.tick(true);
    }

    tick(silent = false) {
        while (this.cursor < this.events.length && this.events[this.cursor].t <= this.clock) {
            const { event } = this.events[this.cursor++];
            try {
                this.sink(event, { replay: true, silent });
            } catch (e) {
                console.warn('replay: event failed', event.type, e);
            }
        }
        if (this.el.scrub && !silent) {
            this.el.scrub.value = String(this.duration
                ? Math.round((this.clock / this.duration) * 1000) : 0);
        }
        if (this.el.clockEl) {
            this.el.clockEl.textContent =
                `${this.clock.toFixed(1)}s / ${this.duration.toFixed(1)}s`;
        }
    }

    /** Put the office back to an empty floor before replaying from the top. */
    reset() {
        const hud = window.retroHUD;
        const canvas = window.officeCanvas;
        if (hud) {
            hud.stepBadges.clear();
            hud.renderPlan([], '');
            hud.startMissionMeter();
            hud.stopMissionMeter();
            hud.updateTokens({ total_tokens: 0 });
        }
        if (canvas && canvas.agents) {
            canvas.agents.forEach(a => canvas.updateAgentStatus(a.id, 'idle'));
        }
    }

    stop() {
        this.pause();
        this.active = false;
        this.events = [];
        this.cursor = 0;
        this.clock = 0;
        this.duration = 0;
    }

    /** Hand the office back to the live socket. */
    exit() {
        this.stop();
        this.reset();
        if (this.el.play) this.el.play.disabled = true;
        if (this.el.scrub) { this.el.scrub.disabled = true; this.el.scrub.value = '0'; }
        if (this.el.exit) this.el.exit.style.display = 'none';
        if (this.el.select) this.el.select.value = '';
        if (this.el.clockEl) this.el.clockEl.textContent = '—';
        if (this.el.card) this.el.card.dataset.active = 'false';
        this.status('Live. Dispatch a mission, or pick a past one to replay.');
    }
}

window.TraceReplay = TraceReplay;
