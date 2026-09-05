/**
 * AI Agent HQ: Main Application Relay & Simulation Engine
 */

document.addEventListener('DOMContentLoaded', () => {
    const canvas = new PixelOfficeCanvas('officeCanvas');
    const hud = new RetroHUD();
    // canvas.js logs through the HUD; hud.js focuses the camera on a
    // selected agent. Each reaches the other through these.
    window.retroHUD = hud;
    window.officeCanvas = canvas;

    let socket = null;
    let reconnectAttempts = 0;
    // Built after handleEvent is hoisted; the driver feeds recorded events
    // into exactly the same function the socket does.
    let replay = null;

    const promptForm = document.getElementById('promptForm');
    const promptInput = document.getElementById('promptInput');
    const emergencyStopBtn = document.getElementById('emergencyStopBtn');

    // 1. Establish WebSocket Connection with Backend Server
    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        socket = new WebSocket(wsUrl);

        socket.onopen = () => {
            reconnectAttempts = 0;
            hud.addTerminalLog('Connected to AI Agent HQ Network.', 'SUCCESS');
        };

        socket.onclose = () => {
            reconnectAttempts++;
            setTimeout(connectWebSocket, Math.min(3000 * reconnectAttempts, 10000));
        };

        socket.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                handleEvent(data);
            } catch (e) {
                console.error('Error parsing event:', e);
            }
        };
    }

    // 2. Handle Real-Time Events
    //
    // `opts.replay` marks an event synthesised from a recorded trace rather
    // than received from the socket, and `opts.silent` marks one being caught
    // up after a scrub — real, but not worth animating a two-second walk for.
    // Everything else in here is the same path either way, which is the point:
    // replay drives the office through the live renderer instead of a second
    // one that would drift.
    function handleEvent(event, opts = {}) {
        const type = event.type;
        const agentId = event.agent_id;
        const payload = event.data || {};

        // A loaded replay owns the office. Live events would interleave with
        // recorded ones and the floor would show two missions at once.
        if (replay && replay.active && !opts.replay) {
            if (type !== 'system.status') return;
        }
        // The mission id lives on the ENVELOPE, not in data: task.completed
        // carries only {result, artifacts, duration_seconds, ...}.
        const taskId = event.task_id;

        switch (type) {
            case 'system.status':
                if (payload.agents) {
                    canvas.setAgents(payload.agents);
                    hud.registerAgents(payload.agents);
                }
                break;

            case 'task.created':
                hud.addTerminalLog(`Task accepted: '${payload.description}'`, 'ANALYSIS');
                break;

            case 'task.assigned':
                // The boss walks the work over rather than firing a dot across
                // the room. The bubble below carries what the task actually is.
                canvas.handOffTask('orchestrator', payload.assigned_to);
                canvas.updateAgentStatus(payload.assigned_to, 'active');
                // The one thing that pops a bubble over an agent: the work it
                // has just been handed, in the words the message carries.
                canvas.showSpeech(payload.assigned_to, payload.subtask_description);
                hud.setAgentState(payload.assigned_to, 'active', { progress: 0.1 });
                hud.addTerminalLog(`${payload.agent_name || payload.assigned_to} started task '${payload.subtask_description}'`, 'ANALYSIS');
                break;

            case 'agent.status':
                canvas.updateAgentStatus(agentId, payload.status);
                hud.setAgentState(agentId, payload.status, payload);
                break;

            case 'agent.speaking':
                canvas.showSpeech(agentId, payload.speech);
                hud.addTerminalLog(`${payload.agent_name || agentId}: "${payload.speech}"`, 'INFO');
                break;

            // One attendee's turn at the conference table. Reuses the speech
            // bubble rather than inventing a second way of showing an agent
            // saying something — the argument going round the table IS the
            // multi-agent moment, and it should look like talking.
            case 'agent.argued':
                {
                    const said = payload.position || '';
                    canvas.showSpeech(agentId, said.slice(0, 90));
                    const tag = payload.simulated ? ' [simulated]' : '';
                    hud.addTerminalLog(
                        `${payload.agent_name || agentId} objects${tag}: "${said}"`, 'ANALYSIS');
                }
                break;

            case 'tool.called':
                hud.addTerminalLog(`Executing ${payload.tool}(${JSON.stringify(payload.arguments)})`, 'ANALYSIS');
                break;

            case 'tool.result':
                hud.addTerminalLog(`Finished ${payload.tool} (${payload.duration_ms}ms)`, 'SUCCESS');
                break;

            case 'terminal.log':
                hud.addTerminalLog(payload.message, payload.level === 'SUCCESS' ? 'SUCCESS' : (payload.level === 'ERROR' ? 'ERROR' : 'INFO'));
                break;

            case 'token.update':
                hud.updateTokens(payload);
                break;

            // ---- failures -------------------------------------------------
            // Without this a failed step was indistinguishable from one still
            // thinking: the backend emitted task.failed and nothing listened.
            case 'task.failed':
                canvas.updateAgentStatus(agentId, 'error');
                hud.setAgentState(agentId, 'error', payload);
                hud.addTerminalLog(
                    `FAILED ${payload.step_id || 'step'}: ${payload.error || 'unknown error'}`,
                    'ERROR');
                break;

            // {completed_step, total_steps, wave, failed}
            case 'task.progress':
                hud.addTerminalLog(
                    `Step ${payload.completed_step} of ${payload.total_steps} done` +
                    (payload.failed ? ` (${payload.failed} failed)` : ''), 'INFO');
                break;

            // ---- the floor ------------------------------------------------
            // office.py decides an agent must stand at a station before its
            // tool will run. These two are what make the office show that.
            case 'agent.move':
                if (payload.to) {
                    // A scrub lands the agent where it ended up; walking it
                    // there would take seconds the viewer just skipped past.
                    if (opts.silent) canvas.agentArrived(agentId, payload.to.x, payload.to.y);
                    else canvas.walkAgentTo(agentId, payload.to.x, payload.to.y,
                                            payload.label ? `to the ${payload.label}` : null);
                    hud.addTerminalLog(
                        `${payload.agent_name || agentId} → ${payload.label || 'station'}`, 'INFO');
                }
                break;

            case 'agent.arrived':
                if (payload.at) canvas.agentArrived(agentId, payload.at.x, payload.at.y);
                break;

            // {agent_name, label, station_id, tool, occupied_by, queue_length}
            case 'station.queued':
                hud.addTerminalLog(
                    `${payload.agent_name || agentId} waiting for ${payload.label}` +
                    (payload.occupied_by ? ` (held by ${payload.occupied_by})` : ''), 'INFO');
                break;

            case 'station.acquired':
                hud.addTerminalLog(
                    `${payload.agent_name || agentId} has ${payload.label || payload.station_id}`,
                    'SUCCESS');
                break;

            case 'station.released':
                break;

            // ---- the plan -------------------------------------------------
            // plan.created carries {goal, steps, source}; dag.update carries
            // {dag, source} -- the same nodes, restated after every step. Both
            // just redraw the panel.
            case 'plan.created':
                hud.setMissionGoal(payload.goal);
                // A new plan is a new meter: badges, cost and the clock all
                // belong to one mission and none of them carry over.
                hud.startMissionMeter();
                hud.renderPlan(payload.steps || [], payload.source);
                hud.addTerminalLog(
                    `Plan: ${(payload.steps || []).length} steps (${payload.source || 'planner'})`,
                    'ANALYSIS');
                break;

            case 'dag.update':
                hud.renderPlan(payload.dag || [], payload.source);
                // The same event now carries what each finished step cost, so
                // the cost panel reads the graph rather than keeping its own
                // ledger that could disagree with it.
                hud.updateCost(payload);
                break;

            case 'route.changed':
                hud.addTerminalLog(
                    `Rerouted ${payload.step_id || 'step'} → ${payload.to_agent_name || payload.to_agent || '?'}`,
                    'ANALYSIS');
                // Memory overrode the planner. That is the office learning, and
                // it is invisible unless the node it happened to says so.
                hud.markStep(payload.step_id, {
                    routed: {
                        from: payload.from_agent_name || payload.from_agent_id || '?',
                        to: payload.to_agent_name || payload.to_agent_id || '?',
                    }
                });
                break;

            // ---- work passed between agents --------------------------------
            // {author_name, name, size_chars, step_id, preview}
            case 'artifact.created':
                hud.addTerminalLog(
                    `${payload.author_name || agentId} produced ${payload.name} (${payload.size_chars} chars)`,
                    'SUCCESS');
                break;

            // {from_agent_name, to_agent_name, name, size_chars, step_id}
            case 'artifact.delivered':
                hud.addTerminalLog(
                    `${payload.name} — ${payload.from_agent_name} → ${payload.to_agent_name}`,
                    'INFO');
                break;

            // `episodes` is a count, not a list.
            case 'memory.recalled':
                if (payload.episodes) {
                    hud.addTerminalLog(
                        `${payload.agent_name || agentId} recalled ${payload.episodes} similar episode(s)`,
                        'INFO');
                }
                break;

            // An artifact was checked before anything downstream read it.
            // {step_id, verdict, score, reasons[], attempt, source, repairing}
            case 'step.critiqued':
                {
                    const accepted = payload.verdict === 'accept';
                    const score = (payload.score === null || payload.score === undefined)
                        ? '' : ` (${Math.round(payload.score * 100)}%)`;
                    const why = (payload.reasons && payload.reasons.length)
                        ? ` — ${payload.reasons[0]}` : '';
                    // A rejected step that is NOT being repaired has spent its
                    // budget and completes anyway. That reads as an accept
                    // unless it is called out, so it is logged as an error.
                    const level = accepted ? 'SUCCESS' : (payload.repairing ? 'ANALYSIS' : 'ERROR');
                    const verb = accepted
                        ? 'accepted'
                        : (payload.repairing ? 'sent back for revision' : 'rejected, no retries left');
                    hud.addTerminalLog(
                        `${payload.step_id} ${verb}${score} by ${payload.source}${why}`,
                        level);
                    // The verdict rides on the node it judged. A capped repair
                    // -- rejected, but completing anyway -- is the case worth
                    // seeing on the graph rather than only in the log.
                    hud.markStep(payload.step_id, {
                        critique: {
                            verdict: payload.verdict,
                            score: (payload.score === undefined ? null : payload.score),
                            repairing: Boolean(payload.repairing),
                            reason: (payload.reasons && payload.reasons[0]) || '',
                        }
                    });
                    canvas.showSpeech(
                        agentId,
                        accepted ? `Work accepted${score}` : 'Reviewer wants changes');
                }
                break;

            case 'task.completed':
                // A mission reaches this event whatever its steps did. Calling a
                // run where everything failed a SUCCESS is how a rate-limited
                // mission came to report itself as finished.
                {
                    const failed = payload.steps_failed || 0;
                    const done = payload.steps_completed || 0;
                    hud.addTerminalLog(
                        failed
                            ? `Task ended in ${payload.duration_seconds}s — ${done} step(s) completed, ${failed} failed`
                            : `Task completed in ${payload.duration_seconds}s`,
                        failed ? 'ERROR' : 'SUCCESS');
                }
                hud.stopMissionMeter();
                // A replayed mission has no report to show: the briefing lives
                // in the mission directory, not in the trace, and inventing one
                // is exactly what a record must not do.
                if (payload.replay) {
                    hud.addTerminalLog(
                        'Replay finished. The briefing itself is in the mission directory.',
                        'INFO');
                    break;
                }
                // The whole payload, not just the text: `artifacts` carries the
                // produced files and their bodies for the FILES tab.
                hud.showExecutiveMemo(payload.result, payload);
                break;
        }
    }

    // 3. User Task Submission
    promptForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const text = promptInput.value.trim();
        if (!text) return;

        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
                action: 'submit_task',
                message: text
            }));
        } else {
            // Standalone fallback simulation if server is not connected
            simulateStandaloneTask(text);
        }

        promptInput.value = '';
    });

    // 4. Emergency Stop
    if (emergencyStopBtn) {
        emergencyStopBtn.addEventListener('click', () => {
            if (socket && socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ action: 'emergency_stop' }));
            }
            hud.addTerminalLog('⚠️ EMERGENCY HALT TRIGGERED — All agent tasks stopped.', 'ERROR');
            canvas.agents.forEach(a => canvas.updateAgentStatus(a.id, 'done'));
        });
    }

    // Standalone Simulation Loop

    /**
     * The offline demo, for when there is no socket to drive the office.
     *
     * Every name and line in here used to be hardcoded — Alice searching, Bob
     * on the database, Eve compiling — so renaming an agent or adding a sixth
     * left the demo describing a roster that no longer existed, and confidently.
     * It now reads whoever is actually in the dock: the backend's manifests when
     * `/api/agents` has answered, and hud.js's DEFAULT_AGENTS before that.
     *
     * The verb comes from each agent's own manifest (`plate_verb`), which is the
     * one line of copy the manifest already carries for exactly this purpose.
     */
    function simulateStandaloneTask(text) {
        hud.addTerminalLog(`User dispatched: '${text}'`, 'ANALYSIS');
        canvas.showSpeech('orchestrator', 'Routing subtasks...');
        canvas.updateAgentStatus('orchestrator', 'thinking');
        hud.setAgentState('orchestrator', 'thinking', { progress: 0.15 });

        // The boss plans rather than executes, so it is not one of the workers.
        const roster = [...hud.agents.values()].filter(a => a.id !== 'orchestrator');

        if (!roster.length) {
            hud.addTerminalLog('No agents in the roster to simulate.', 'ERROR');
            return;
        }

        const STEP_MS = 1200;
        roster.forEach((agent, i) => {
            setTimeout(() => {
                canvas.dispatchTaskOrb('orchestrator', agent.id);
                canvas.updateAgentStatus(agent.id, 'active');
                hud.setAgentState(agent.id, 'active', { progress: (i + 1) / roster.length });
                hud.addTerminalLog(`${agent.name}: ${agent.verb}…`, 'ANALYSIS');
                hud.updateTokens({
                    total_tokens: Math.round(((i + 1) / roster.length) * 7854),
                    max_tokens: 10000
                });
            }, 1000 + i * STEP_MS);
        });

        setTimeout(() => {
            canvas.agents.forEach(a => canvas.updateAgentStatus(a.id, 'done'));
            hud.agents.forEach(a => hud.setAgentState(a.id, 'done', { progress: 1 }));
            hud.addTerminalLog('Simulation complete.', 'SUCCESS');
            hud.showExecutiveMemo(
                `# 🎯 Offline Simulation\n\n**Goal:** ${text}\n\n` +
                roster.map(a => `- **${a.name}**: ${a.verb}.`).join('\n') +
                `\n\n_No backend is connected, so nothing above actually ran._ ` +
                `This is the office animating against its own roster. Start the ` +
                `server and dispatch again for a real mission.`);
        }, 1000 + roster.length * STEP_MS + 800);
    }

    // The replay driver. Constructed before the socket so a trace loaded
    // straight away is already holding the floor when the first live event
    // arrives.
    if (window.TraceReplay) {
        replay = new TraceReplay((event, opts) => handleEvent(event, opts));
        window.traceReplay = replay;
    }

    // Try initial connection
    connectWebSocket();
});
