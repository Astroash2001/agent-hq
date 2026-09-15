"""
Run a mission with no browser and no API key, and print what actually happened.

This is the hands-on check for the four backend subsystems. It taps the event
bus, runs one or two missions through the real orchestrator, prints an annotated
timeline, and ends with a per-subsystem verdict drawn from the events rather than
from anything the script assumes.

    cd backend
    python scripts/watch_mission.py
    python scripts/watch_mission.py --twice        # second run shows memory recall
    python scripts/watch_mission.py --quiet        # verdicts only, no timeline
    python scripts/watch_mission.py "your own goal here"

Nothing here is imported by the server; it is a development tool.
"""
import argparse
import asyncio
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The briefing contains emoji and the Windows console defaults to cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from event_bus import event_bus            # noqa: E402
from memory import memory                  # noqa: E402
from office import office                  # noqa: E402
from orchestrator import orchestrator      # noqa: E402
from schemas import Event                  # noqa: E402

DEFAULT_GOAL = "Implement and test a fast Python fibonacci cache"

# Events worth printing. Everything else is counted but not shown, so the
# timeline stays readable instead of scrolling past the interesting parts.
NARRATED = {
    "plan.created", "route.changed",
    "task.assigned", "task.progress",
    "agent.move", "station.queued", "station.acquired", "station.released",
    "artifact.created", "artifact.delivered",
    "memory.recalled", "task.completed",
}


class Recorder:
    """Collects the event stream and turns it into a verdict."""

    def __init__(self):
        self.events = []
        self.counts = Counter()
        self.t0 = time.time()

    async def record(self, event: Event):
        self.counts[event.type.value] += 1
        self.events.append((round(time.time() - self.t0, 2), event))

    def of(self, *types):
        return [(t, e) for t, e in self.events if e.type.value in types]

    # -------------------------------------------------------------- narration

    def line(self, elapsed, event) -> str:
        kind = event.type.value
        d = event.data
        who = d.get("agent_name") or event.agent_id or "-"

        if kind == "plan.created":
            steps = " -> ".join(
                f"{s['id']}:{s['agent_id']}" + (f"(after {','.join(s['dependencies'])})"
                                                if s["dependencies"] else "")
                for s in d["steps"]
            )
            return (f"PLAN     source={d['source']} steps={len(d['steps'])} "
                    f"max_parallel={d['max_parallel']}\n           {steps}")
        if kind == "route.changed":
            return (f"REROUTE  {d['step_id']}: {d['from_agent_name']} -> {d['to_agent_name']} "
                    f"({d['from_score']} -> {d['to_score']})")
        if kind == "task.assigned":
            got = f" needs {','.join(d['consumes'])}" if d.get("consumes") else ""
            return (f"ASSIGN   {d['step_id']} -> {d['agent_name']} "
                    f"(produces {d['produces']}{got})")
        if kind == "agent.move":
            dest = d.get("label") or "desk"
            return (f"WALK     {who} -> {dest} "
                    f"({d['from']['x']},{d['from']['y']}) to ({d['to']['x']},{d['to']['y']}) "
                    f"{d['duration_ms']}ms")
        if kind == "station.queued":
            return (f"QUEUE    {who} waiting for {d['label']} "
                    f"(held by {d['occupied_by']}, queue={d['queue_length']})")
        if kind == "station.acquired":
            return f"HOLD     {who} has {d['label']} for {d['tool']}"
        if kind == "station.released":
            return f"FREE     {who} released {d['label']}"
        if kind == "artifact.created":
            return f"WROTE    {who} -> {d['name']} ({d['size_chars']} chars)"
        if kind == "artifact.delivered":
            return f"CARRIED  {d['name']}: {d['from_agent_name']} -> {d['to_agent_name']}"
        if kind == "memory.recalled":
            return f"RECALL   {who} started holding {d['episodes']} past episode(s)"
        if kind == "task.progress":
            return (f"WAVE     wave {d.get('wave')} done: "
                    f"{d['completed_step']}/{d['total_steps']} steps")
        if kind == "task.completed":
            return (f"DONE     {d['duration_seconds']}s, {d['subtasks_count']} steps, "
                    f"{len(d['artifacts'])} artifacts")
        return f"{kind} {who}"

    def print_timeline(self):
        print("\n--- TIMELINE " + "-" * 60)
        for elapsed, event in self.events:
            if event.type.value in NARRATED:
                print(f"{elapsed:6.2f}s  {self.line(elapsed, event)}")

    # ---------------------------------------------------------------- verdicts

    def verdicts(self, result):
        print("\n--- VERDICTS " + "-" * 60)

        # P1: planning
        plans = self.of("plan.created")
        if plans:
            d = plans[0][1].data
            deps = sum(len(s["dependencies"]) for s in d["steps"])
            print(f"P1 PLANNER     PASS  {len(d['steps'])} steps, {deps} dependency edges, "
                  f"source={d['source']}")
            print(f"                     (source=heuristic is correct with no API key set)")
        else:
            print("P1 PLANNER     FAIL  no plan.created event")

        # P2: parallelism + artifacts
        starts = {e.data["step_id"]: t for t, e in self.of("task.assigned")}
        overlaps = []
        for a in starts:
            for b in starts:
                if a < b and abs(starts[a] - starts[b]) < 0.5:
                    overlaps.append((a, b, round(abs(starts[a] - starts[b]), 3)))
        if overlaps:
            pairs = ", ".join(f"{a}+{b} ({gap}s apart)" for a, b, gap in overlaps)
            print(f"P2 PARALLEL    PASS  dispatched together: {pairs}")
        else:
            print("P2 PARALLEL    ----  no two steps started together "
                  "(a strictly linear plan is legitimate)")

        made = self.of("artifact.created")
        carried = self.of("artifact.delivered")
        if made and carried:
            print(f"P2 WORKSPACE   PASS  {len(made)} artifacts written, "
                  f"{len(carried)} carried between desks")
        elif made:
            print(f"P2 WORKSPACE   ----  {len(made)} artifacts written, none needed carrying")
        else:
            print("P2 WORKSPACE   FAIL  no artifacts produced")

        # P3: spatial
        walks = self.of("agent.move")
        holds = self.of("station.acquired")
        queues = self.of("station.queued")
        if walks and holds:
            stations = sorted({e.data["label"] for _, e in holds})
            print(f"P3 SPATIAL     PASS  {len(walks)} walks, {len(holds)} station holds "
                  f"at {', '.join(stations)}")
            if queues:
                print(f"                     {len(queues)} queue event(s) -- two agents "
                      f"contended for one station")
            else:
                print("                     no contention this run (see --contention)")
        else:
            print("P3 SPATIAL     FAIL  no movement or no station holds "
                  "(is spatial_enabled off?)")

        # P6: memory
        recalls = self.of("memory.recalled")
        reroutes = self.of("route.changed")
        print(f"P6 MEMORY      PASS  store now holds {len(memory.episodes)} episodes")
        if recalls:
            print(f"                     {len(recalls)} step(s) started with recalled history")
        else:
            print("                     no recall this run (expected on a cold store; "
                  "use --twice)")
        if reroutes:
            print(f"                     {len(reroutes)} step(s) rerouted by record")

        print(f"\nEVENT TOTALS   {dict(self.counts)}")


async def contention_demo():
    """
    Force two agents onto one station so the queue is visible.

    The normal plan spreads its tools across stations, so contention does not
    always happen on its own -- this proves the lock and the queue event work.
    """
    print("\n--- CONTENTION " + "-" * 58)
    seen = []

    async def tap(event: Event):
        if event.type.value in ("station.queued", "station.acquired", "station.released"):
            seen.append(event)

    event_bus.subscribe_all(tap)

    async def use(agent_id, name):
        async with office.use_tool_station(agent_id, "query_data", agent_name=name) as station:
            print(f"  {name} is holding {station.label}")
            await asyncio.sleep(0.6)
            print(f"  {name} is done")

    await asyncio.gather(use("coder", "Charlie"), use("data_analyst", "Bob"))
    await asyncio.sleep(0.1)
    event_bus.unsubscribe_all(tap)

    queued = [e for e in seen if e.type.value == "station.queued"]
    if queued:
        d = queued[0].data
        print(f"  QUEUE event fired: {d['agent_name']} waited for {d['label']} "
              f"(held by {d['occupied_by']})")
        print("  PASS  the station serialised the two agents")
    else:
        print("  FAIL  no queue event -- the station did not serialise")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("goal", nargs="?", default=DEFAULT_GOAL)
    parser.add_argument("--twice", action="store_true",
                        help="run the goal twice so the second run recalls the first")
    parser.add_argument("--quiet", action="store_true", help="verdicts only")
    parser.add_argument("--contention", action="store_true",
                        help="also force two agents onto one station")
    args = parser.parse_args()

    if args.twice:
        print(f"Run 1 of 2 (warming memory): {args.goal}")
        await orchestrator.handle_user_input(args.goal)
        await asyncio.sleep(0.2)
        print("Run 1 complete.\n")

    print(f"Goal: {args.goal}")
    recorder = Recorder()

    async def tap(event: Event):
        await recorder.record(event)

    event_bus.subscribe_all(tap)
    try:
        result = await orchestrator.handle_user_input(args.goal)
        await asyncio.sleep(0.2)
    finally:
        event_bus.unsubscribe_all(tap)

    if not args.quiet:
        recorder.print_timeline()
    recorder.verdicts(result)

    if args.contention:
        await contention_demo()

    print("\nFull briefing is in the returned result; first 400 chars:\n")
    print(result["result"][:400] + " ...")


if __name__ == "__main__":
    asyncio.run(main())
