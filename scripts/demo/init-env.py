#!/usr/bin/env python3
"""Create a local demo configuration without overwriting existing credentials."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[2]
target = root / '.env'
values = {key: secrets.token_urlsafe(36) for key in (
    'POSTGRES_PASSWORD', 'JWT_SECRET_KEY', 'ENCRYPTION_MASTER_KEY',
    'AGENT_SHARED_TOKEN', 'DEMO_PASSWORD',
)}
with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
    stream.write('# Generated local demo credentials. Keep this file private.\n')
    for key, value in values.items():
        stream.write(f'{key}={value}\n')
print('Created .env (owner read/write). Demo login: demo@example.org; password is DEMO_PASSWORD in .env.')
