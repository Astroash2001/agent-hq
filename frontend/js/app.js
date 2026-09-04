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
    function handleEvent(event) {
        const type = event.type;
        const agentId = event.agent_id;
        const payload = event.data || {};
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
                    canvas.walkAgentTo(agentId, payload.to.x, payload.to.y,
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
                hud.renderPlan(payload.steps || [], payload.source);
                hud.addTerminalLog(
                    `Plan: ${(payload.steps || []).length} steps (${payload.source || 'planner'})`,
                    'ANALYSIS');
                break;

            case 'dag.update':
                hud.renderPlan(payload.dag || [], payload.source);
                break;

            case 'route.changed':
                hud.addTerminalLog(
                    `Rerouted ${payload.step_id || 'step'} → ${payload.to_agent_name || payload.to_agent || '?'}`,
                    'ANALYSIS');
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
    let standaloneTaskSeq = 50;

    function simulateStandaloneTask(text) {
        const taskId = `#${++standaloneTaskSeq}`;
        hud.addTerminalLog(`User dispatched: '${text}'`, 'ANALYSIS');
        canvas.showSpeech('orchestrator', 'Routing subtasks...');
        canvas.updateAgentStatus('orchestrator', 'thinking');
        hud.setAgentState('orchestrator', 'thinking', { progress: 0.15 });

        const steps = [
            { at: 1000, id: 'researcher',   log: 'Alice: Searching knowledge base…',        level: 'ANALYSIS' },
            { at: 2200, id: 'coder',        log: 'Charlie: Running AST syntax checks…',     level: 'ANALYSIS' },
            { at: 3400, id: 'data_analyst', log: "Bob started 'Task #049'",                 level: 'ANALYSIS' },
            { at: 4600, id: 'creative',     log: 'Eve: Compiling synthesis report.',        level: 'ANALYSIS' }
        ];

        steps.forEach((step, i) => {
            setTimeout(() => {
                canvas.dispatchTaskOrb('orchestrator', step.id);
                canvas.updateAgentStatus(step.id, 'active');
                hud.setAgentState(step.id, 'active', { progress: (i + 1) / steps.length });
                hud.addTerminalLog(step.log, step.level);
                hud.updateTokens({
                    total_tokens: Math.round(((i + 1) / steps.length) * 7854),
                    max_tokens: 10000
                });
            }, step.at);
        });

        setTimeout(() => {
            canvas.agents.forEach(a => canvas.updateAgentStatus(a.id, 'done'));
            hud.agents.forEach(a => hud.setAgentState(a.id, 'done', { progress: 1 }));
            hud.addTerminalLog('Task successfully completed!', 'SUCCESS');
            hud.showExecutiveMemo(
                `# 🎯 Task Execution Summary\n\n**Goal:** ${text}\n\n` +
                `- **Alice (Research)**: Technical references & vector matches retrieved.\n` +
                `- **Charlie (Code)**: AST syntax verified with 0 linting errors.\n` +
                `- **Bob (Data)**: Database state synchronized.\n` +
                `- **Eve (Synthesis)**: Report compiled and verified.\n\n` +
                `*Status: 100% Operational & Verified.*`);
        }, 6000);
    }

    // Try initial connection
    connectWebSocket();
});
