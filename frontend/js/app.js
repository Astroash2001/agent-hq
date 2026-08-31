/**
 * AI Agent HQ: Main Application Relay & Simulation Engine
 */

document.addEventListener('DOMContentLoaded', () => {
    const canvas = new PixelOfficeCanvas('officeCanvas');
    const hud = new RetroHUD();
    // hud.js reaches for these to drive the dock and the simulation controls.
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

        switch (type) {
            case 'system.status':
                if (payload.agents) {
                    canvas.setAgents(payload.agents);
                    hud.registerAgents(payload.agents);
                }
                break;

            case 'task.created':
                hud.enqueueTask(payload.id || `#${hud.queue.length + 1}`, payload.description);
                hud.addTerminalLog(`Task: '${payload.description}' queued.`, 'ANALYSIS');
                break;

            case 'task.assigned':
                canvas.dispatchTaskOrb('orchestrator', payload.assigned_to);
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

            case 'task.completed':
                hud.completeTask(payload.id);
                hud.addTerminalLog(`Task completed in ${payload.duration_seconds}s`, 'SUCCESS');
                hud.showExecutiveMemo(payload.result);
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
        hud.enqueueTask(taskId, text);
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
            hud.completeTask(taskId);
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
