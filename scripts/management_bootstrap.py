#!/usr/bin/env python3
"""Configure project-specific Vault paths using an existing administrative session."""
import argparse
import getpass
import json
import os
import re
from pathlib import Path
import sys
from vault_runtime import Vault

ROOT = Path(__file__).resolve().parents[1]


class BootstrapError(ValueError):
    pass


def step(vault, label, path, method='GET', body=None):
    try:
        return vault.call(path, method, body)
    except Exception as error:
        match = re.fullmatch(r'Secret service request failed \(HTTP (\d+)\)', str(error))
        status = 'HTTP '+match.group(1) if match else type(error).__name__
        hint = ' Check token validity and permissions.' if match and match.group(1) in ('401', '403') else ''
        raise BootstrapError(f'{label} failed ({status}).{hint} Response content suppressed.') from None


def configure(vault):
    def call(path, method='GET', body=None):
        return step(vault, method+' '+path, path, method, body)
    mounts = call('sys/mounts')['data']
    if 'ping-eks/' not in mounts:
        call('sys/mounts/ping-eks', 'POST', {'type': 'kv', 'options': {'version': '2'}})
    elif mounts['ping-eks/']['type'] != 'kv' or mounts['ping-eks/'].get('options', {}).get('version') != '2':
        raise BootstrapError('Existing ping-eks mount is not KV v2; refusing replacement')
    auth = call('sys/auth')['data']
    if 'ping-eks-github/' not in auth:
        call('sys/auth/ping-eks-github', 'POST', {'type': 'jwt'})
    elif auth['ping-eks-github/']['type'] != 'jwt':
        raise BootstrapError('Existing project auth mount has an unexpected type')
    call('auth/ping-eks-github/config', 'POST', {
        'oidc_discovery_url': 'https://token.actions.githubusercontent.com',
        'bound_issuer': 'https://token.actions.githubusercontent.com'})
    role = json.loads((ROOT/'management/vault-role.example.json').read_text())
    for name, file in [('ping-eks-restore', 'vault-read.hcl'), ('ping-eks-backup', 'vault-backup.hcl')]:
        call('sys/policies/acl/'+name, 'PUT', {'policy': (ROOT/'management'/file).read_text()})
        call('auth/ping-eks-github/role/'+name, 'POST', {**role, 'token_policies': [name]})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt-token', action='store_true', help='Read a temporary token without echo or shell variables')
    args = parser.parse_args()
    try:
        if not os.environ.get('VAULT_ADDR'):
            raise BootstrapError('VAULT_ADDR is missing. Set it to the local forwarded endpoint.')
        token = getpass.getpass('Temporary Vault administrative token: ') if args.prompt_token else os.environ.get('VAULT_TOKEN')
        if not token or not token.strip():
            raise BootstrapError('No token supplied. Run with --prompt-token.')
        try:
            vault = Vault(token=token.strip())
        except ValueError:
            raise BootstrapError('Unsupported Vault address. Use HTTPS or the documented local HTTP endpoint.') from None
        configure(vault)
        print('Project KV v2 mount, GitHub JWT roles and policies configured. Existing runtime values unchanged.')
    except BootstrapError as error:
        sys.exit('Vault bootstrap failed: '+str(error))
    except Exception as error:
        sys.exit('Vault bootstrap failed ('+type(error).__name__+'). Details suppressed to protect credentials.')
