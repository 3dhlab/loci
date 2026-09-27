#!/usr/bin/env python3
"""Collect bounded demo diagnostics while keeping local credentials private."""
import json
from pathlib import Path
import subprocess

secrets = []
if Path('.env').exists():
    secrets = [line.split('=', 1)[1] for line in Path('.env').read_text().splitlines()
               if '=' in line and not line.startswith('#') and len(line.split('=', 1)[1]) >= 8]

def capture(label, command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    for secret in secrets:
        output = output.replace(secret, '[redacted]')
    print('\n' + label + '\n' + output)

capture('Runner disk space', ['df', '-h'])
capture('Demo service state', ['docker', 'compose', 'ps', '-a'])
capture('API logs', ['docker', 'compose', 'logs', '--no-color', '--tail', '100', 'api'])
ids = subprocess.run(['docker', 'compose', 'ps', '-aq', 'api'], capture_output=True, text=True).stdout.split()
for container in ids:
    capture('API health checks', ['docker', 'inspect', '--format', '{{json .State.Health}}', container])
capture('API health response', ['docker', 'compose', 'exec', '-T', 'api', 'python', '-c',
    "from app.services.runtime_health import build_runtime_health_snapshot; import json; print(json.dumps(build_runtime_health_snapshot(), indent=2))"])
