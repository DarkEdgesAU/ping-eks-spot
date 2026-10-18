#!/usr/bin/env python3
"""Versioned runtime secret delivery. Never print request bodies or credentials."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def generator_guard():
    if os.environ.get('PING_SECRET_SOURCE') == 'vault' or (ROOT/'.secrets/vault-managed').exists():
        raise ValueError('Vault owns runtime secrets. Use vault_runtime.py; generation is disabled.')


def request(url, method='GET', body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        # Vault errors can include supplied values. Do not expose their bodies.
        raise ValueError(f'Secret service request failed (HTTP {error.code})') from None


class Vault:
    def __init__(self, address=None, token=None):
        self.address = (address or os.environ['VAULT_ADDR']).rstrip('/')
        parsed = urllib.parse.urlparse(self.address)
        local_http = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
        if parsed.scheme != 'https' and not local_http and self.address != 'http://vault.hashicorp-vault.svc.cluster.local:8200':
            raise ValueError('HTTP is allowed only for the explicitly selected local Vault service')
        self.token = token or os.environ.get('VAULT_TOKEN')
        self.owned_token = False

    def call(self, path, method='GET', body=None):
        return request(self.address+'/v1/'+path, method, body,
                       {'X-Vault-Token': self.token} if self.token else {})

    def login(self, role, audience, mount='ping-eks-github'):
        if self.token:
            return
        endpoint = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
        separator = '&' if '?' in endpoint else '?'
        jwt = request(endpoint+separator+urllib.parse.urlencode({'audience': audience}),
                      headers={'Authorization': 'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})['value']
        self.token = self.call(f'auth/{mount}/login', 'POST', {'role': role, 'jwt': jwt})['auth']['client_token']
        self.owned_token = True

    def close(self):
        if self.owned_token:
            self.call('auth/token/revoke-self', 'POST', {})
            self.token = None


def validate_secret(secret):
    if secret.get('kind') != 'Secret' or secret.get('apiVersion') != 'v1':
        raise ValueError('Expected a Kubernetes Secret')
    for field in ('namespace', 'name'):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', secret['metadata'][field]):
            raise ValueError('Invalid Secret identity')
    for value in secret.get('data', {}).values():
        base64.b64decode(value, validate=True)
    if 'stringData' in secret:
        raise ValueError('Import canonical base64 data, not stringData')
    return secret


def import_secrets(vault, mount, prefix, secrets):
    records = []
    for secret in secrets:
        validate_secret(secret)
        path = prefix+'/runtime/'+secret['metadata']['namespace']+'/'+secret['metadata']['name']
        current = None
        try:
            current = vault.call(f'{mount}/data/{path}')['data']
        except ValueError as error:
            if 'HTTP 404' not in str(error):
                raise
        if current and current['data'] == secret:
            version = current['metadata']['version']
        else:
            version = vault.call(f'{mount}/data/{path}', 'POST', {
                'options': {'cas': current['metadata']['version'] if current else 0},
                'data': secret})['data']['version']
        records.append({'mount': mount, 'path': path, 'version': version,
                        'namespace': secret['metadata']['namespace'], 'name': secret['metadata']['name']})
    return records


def read_secrets(vault, records):
    result = []
    for record in records:
        version = record['version']
        if not isinstance(version, int) or version < 1:
            raise ValueError('An exact KV version is required')
        data = vault.call(f"{record['mount']}/data/{record['path']}?version={version}")['data']
        if data['metadata']['version'] != version:
            raise ValueError('Vault returned an unexpected version')
        secret = validate_secret(data['data'])
        if (secret['metadata']['namespace'], secret['metadata']['name']) != (record['namespace'], record['name']):
            raise ValueError('Vault Secret identity mismatch')
        result.append(secret)
    return result


def import_bootstrap_files(vault, files):
    path = 'ping-eks/data/lab/bootstrap/files'
    try:
        current = vault.call(path)['data']
    except ValueError as error:
        if 'HTTP 404' not in str(error):
            raise
        current = None
    if current and current['data'] == {'files': files}:
        return current['metadata']['version']
    return vault.call(path, 'POST', {'options': {'cas': current['metadata']['version'] if current else 0},
                                    'data': {'files': files}})['data']['version']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['import', 'verify'])
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--index', type=Path, required=True)
    args = parser.parse_args()
    vault = Vault()
    try:
        if args.command == 'import':
            import recovery
            docs, _ = recovery.prepare(args.inventory)
            records = import_secrets(vault, 'ping-eks', 'lab', docs['20-runtime-secrets.yaml'])
            args.index.parent.mkdir(parents=True, exist_ok=True)
            args.index.write_text(json.dumps(records, indent=2)+'\n')
        else:
            read_secrets(vault, json.loads(args.index.read_text()))
        print('Runtime secret operation complete; no values printed.')
    finally:
        vault.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Runtime secret operation failed. Check access, identities and input files; values suppressed.') from None
