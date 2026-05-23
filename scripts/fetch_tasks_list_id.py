#!/usr/bin/env python3
"""
Fetch Google Tasks list ID (find "My Tasks" or first list) using OAuth refresh token
and write GOOGLE_TASKS_LIST_ID into .env in project root.

Usage: python3 scripts/fetch_tasks_list_id.py

Requires .env in repo root with: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""
import os
import sys
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

def load_env(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k,v = line.split('=',1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def write_env(path, env_updates):
    lines = []
    existing = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line or line.strip().startswith('#') or '=' not in line:
                lines.append(line)
                continue
            k,v = line.split('=',1)
            key = k.strip()
            existing[key] = True
            if key in env_updates:
                lines.append(f"{key}={env_updates[key]}")
            else:
                lines.append(line)
    else:
        # create new
        pass
    # append any new keys not present
    for k,v in env_updates.items():
        if k not in existing:
            lines.append(f"{k}={v}")
    path.write_text('\n'.join(lines)+ ('\n' if lines and not lines[-1].endswith('\n') else ''))


def exchange_refresh_token(client_id, client_secret, refresh_token):
    url = 'https://oauth2.googleapis.com/token'
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }
    data_b = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=data_b, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            return json.loads(body)
    except Exception as e:
        print('Token exchange failed:', e)
        return None


def get_tasklists(access_token):
    url = 'https://tasks.googleapis.com/tasks/v1/users/@me/lists'
    req = urllib.request.Request(url, method='GET')
    req.add_header('Authorization', f'Bearer {access_token}')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            return json.loads(body)
    except Exception as e:
        print('Failed to fetch tasklists:', e)
        return None


def main():
    env = load_env(ENV_PATH)
    # Prefer Tasks-specific vars, fall back to Calendar vars (matches action_tools.py behavior)
    client_id = (
        env.get('GOOGLE_TASKS_CLIENT_ID') or env.get('GOOGLE_CLIENT_ID') or env.get('GOOGLE_OAUTH_CLIENT_ID') or env.get('GOOGLE_CALENDAR_CLIENT_ID')
    )
    client_secret = (
        env.get('GOOGLE_TASKS_CLIENT_SECRET') or env.get('GOOGLE_CLIENT_SECRET') or env.get('GOOGLE_OAUTH_CLIENT_SECRET') or env.get('GOOGLE_CALENDAR_CLIENT_SECRET')
    )
    refresh_token = (
        env.get('GOOGLE_TASKS_REFRESH_TOKEN') or env.get('GOOGLE_REFRESH_TOKEN') or env.get('GOOGLE_OAUTH_REFRESH_TOKEN') or env.get('GOOGLE_CALENDAR_REFRESH_TOKEN')
    )

    if not (client_id and client_secret and refresh_token):
        print('Missing required OAuth values in', ENV_PATH)
        print('Need: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN')
        sys.exit(2)

    print('Exchanging refresh token for access token...')
    tok = exchange_refresh_token(client_id, client_secret, refresh_token)
    if not tok or 'access_token' not in tok:
        print('Failed to obtain access token. Response:', tok)
        sys.exit(3)

    access_token = tok['access_token']
    print('Fetching tasklists...')
    lists = get_tasklists(access_token)
    if not lists or 'items' not in lists:
        print('No tasklists found. Response:', lists)
        sys.exit(4)

    items = lists['items']
    chosen = None
    for it in items:
        title = it.get('title','')
        if title.lower() == 'my tasks':
            chosen = it
            break
    if not chosen and items:
        chosen = items[0]

    if not chosen:
        print('No tasklist items available.')
        sys.exit(5)

    list_id = chosen.get('id')
    title = chosen.get('title')
    print(f'Chosen tasklist: "{title}" id={list_id}')

    # write to .env
    print('Writing GOOGLE_TASKS_LIST_ID to', ENV_PATH)
    write_env(ENV_PATH, {'GOOGLE_TASKS_LIST_ID': list_id})
    print('Done.')

if __name__ == '__main__':
    main()
