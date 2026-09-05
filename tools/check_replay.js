/**
 * Validate the trace-replay compiler against a real mission.
 *
 * `frontend/js/replay.js` turns recorded spans back into the events the
 * WebSocket would have sent, and that translation is the one piece of new
 * frontend logic with no test behind it -- there is no JS test runner in this
 * repo, and adding one would be its first node build step. This is the same
 * kind of hand-run validator as `check_layout.py`: it loads the real module in
 * a bare VM context, feeds it a real `trace.jsonl`, and checks the compiled
 * timeline against the mission manifest that was written beside it.
 *
 *     node tools/check_replay.js                 # newest mission with a trace
 *     node tools/check_replay.js task_a1f6600c   # a specific one
 *
 * Exits non-zero if anything is wrong.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const MISSIONS = path.join(ROOT, 'backend', 'data', 'missions');

function fail(message) {
    console.error(`FAIL  ${message}`);
    process.exitCode = 1;
}

function ok(message) {
    console.log(`ok    ${message}`);
}

/** The newest mission directory that has both a trace and a manifest. */
function newestMission() {
    if (!fs.existsSync(MISSIONS)) return null;
    const candidates = fs.readdirSync(MISSIONS)
        .map(name => path.join(MISSIONS, name))
        .filter(dir => fs.existsSync(path.join(dir, 'trace.jsonl')) &&
                       fs.existsSync(path.join(dir, 'mission.json')))
        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
    return candidates[0] || null;
}

/** Load replay.js with just enough of a browser around it to define the class. */
function loadTraceReplay() {
    const sandbox = {
        window: {}, console,
        document: { getElementById: () => null },
        fetch: async () => { throw new Error('no network in the validator'); },
        performance: { now: () => 0 },
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'frontend', 'js', 'replay.js'), 'utf8'),
                    sandbox, { filename: 'replay.js' });
    if (!sandbox.window.TraceReplay) throw new Error('replay.js did not define TraceReplay');
    return sandbox.window.TraceReplay;
}

function main() {
    const arg = process.argv[2];
    const dir = arg ? path.join(MISSIONS, arg) : newestMission();
    if (!dir || !fs.existsSync(path.join(dir, 'trace.jsonl'))) {
        console.log('No mission with a trace.jsonl to check. Run one first:');
        console.log('  cd backend && python main.py   → dispatch a goal in the office');
        return;
    }
    const missionId = path.basename(dir);
    console.log(`Checking ${missionId}\n`);

    const spans = fs.readFileSync(path.join(dir, 'trace.jsonl'), 'utf8')
        .split('\n').filter(Boolean).map(line => JSON.parse(line));
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'mission.json'), 'utf8'));

    const TraceReplay = loadTraceReplay();
    const driver = Object.create(TraceReplay.prototype);
    driver.missionId = missionId;
    // Two real bindings from office.py, so the walk reconstruction is exercised.
    driver.toolStations = new Map([
        ['web_search', { x: 2.6, y: 12.4, label: 'Research Library', id: 'research_library' }],
        ['write_file', { x: 2.6, y: 9.4, label: 'Filing Archive', id: 'filing_archive' }],
    ]);

    // The shape `GET /api/missions/{id}` hands the driver.
    const mission = {
        id: missionId,
        goal: manifest.goal,
        plan: manifest.plan || { steps: manifest.steps || [] },
        plan_source: manifest.plan_source,
        steps: manifest.steps,
    };

    const events = driver.compile(spans, mission);
    console.log(`${spans.length} spans → ${events.length} events\n`);

    if (!events.length) return fail('the trace compiled to no events at all');

    // 1. Time only moves forward. A replay clock cannot fire an event it
    //    has already passed, so an unsorted timeline silently drops events.
    const monotonic = events.every((e, i) => i === 0 || events[i - 1].t <= e.t);
    monotonic ? ok('events are in non-decreasing time order')
              : fail('events are not sorted by time');

    // 2. The graph has to be on screen before anything runs on it.
    const first = events[0].event.type;
    ['plan.created', 'task.created'].includes(first)
        ? ok(`opens with ${first}`)
        : fail(`opens with ${first}, before any plan is on screen`);

    // 3. Every step in the plan is assigned exactly once.
    const planSteps = (mission.plan.steps || []).map(s => s.id || s.step_id);
    const assigned = events.filter(e => e.event.type === 'task.assigned')
                           .map(e => e.event.data.step_id);
    const missing = planSteps.filter(id => !assigned.includes(id));
    missing.length ? fail(`never assigned: ${missing.join(', ')}`)
                   : ok(`all ${planSteps.length} planned step(s) assigned`);

    // 4. The final graph agrees with the manifest about what happened.
    const dagEvents = events.filter(e => e.event.type === 'dag.update');
    if (!dagEvents.length) return fail('no dag.update was produced');
    const finalDag = dagEvents[dagEvents.length - 1].event.data;

    const recorded = new Map((manifest.steps || []).map(s => [s.id || s.step_id, s.status]));
    let disagreements = 0;
    for (const node of finalDag.dag) {
        const was = recorded.get(node.id);
        if (was && was !== node.status) {
            fail(`${node.id}: manifest says "${was}", replay ends "${node.status}"`);
            disagreements++;
        }
    }
    if (!disagreements) ok('final step statuses match the mission manifest');

    // 5. The mission total is the sum of the steps, not a lifetime counter.
    const summed = finalDag.dag.reduce((a, n) => a + (n.total_tokens || 0), 0);
    Math.abs(summed - finalDag.mission_tokens) < 1
        ? ok(`mission tokens (${finalDag.mission_tokens}) sum the steps`)
        : fail(`mission tokens ${finalDag.mission_tokens} != step sum ${summed}`);

    // 6. The run ends where the trace ends.
    const last = events[events.length - 1].event;
    last.type === 'task.completed'
        ? ok(`closes with task.completed at ${events[events.length - 1].t.toFixed(1)}s ` +
             `(${last.data.steps_completed} completed, ${last.data.steps_failed} failed)`)
        : fail(`closes with ${last.type}, not task.completed`);

    const kinds = {};
    for (const e of events) kinds[e.event.type] = (kinds[e.event.type] || 0) + 1;
    console.log('\nevent mix:', JSON.stringify(kinds));
}

main();
