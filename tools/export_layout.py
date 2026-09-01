"""
Export the office geometry the backend needs, straight out of the map.

`office.py` has to know where the desks and stations are, so it can walk agents
to them. It used to keep its own copy of those coordinates, and that copy went
stale the moment the map was rearranged -- by the time this script was written
every one of the five backend seats had drifted into the middle of a desk
sprite, and two stations sat inside the boardroom table.

So the map owns the geometry and the backend reads it. This writes:

    backend/office_layout.json

Run it whenever seats or stations move in office_map.js. check_layout.py fails
if the file is missing or out of date, so the drift cannot happen quietly again.

    python tools/export_layout.py
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(ROOT, 'frontend', 'js', 'office_map.js')
OUT = os.path.join(ROOT, 'backend', 'office_layout.json')


def read_map():
    """The map through node, so there is never a second parser to keep in step."""
    code = f'const m=require("{MAP.replace(chr(92), "/")}");process.stdout.write(JSON.stringify(m.OFFICE_MAP));'
    r = subprocess.run(['node', '-e', code], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f'node failed reading office_map.js:\n{r.stderr}')
    return json.loads(r.stdout)


def build(m):
    return {
        '_generated_by': 'tools/export_layout.py -- do not edit by hand',
        '_source': 'frontend/js/office_map.js',
        'tile': m['tile'],
        'seats': {k: {'x': v['x'], 'y': v['y'], 'facing': v.get('facing', 'down')}
                  for k, v in m['seats'].items()},
        'stations': {k: {'x': v['x'], 'y': v['y'], 'label': v.get('label', k)}
                     for k, v in m.get('stations', {}).items()},
    }


def main():
    payload = build(read_map())
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.write('\n')
    print(f'wrote {OUT}')
    print(f'  {len(payload["seats"])} seats, {len(payload["stations"])} stations')


if __name__ == '__main__':
    main()
