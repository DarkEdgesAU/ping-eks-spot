#!/usr/bin/env python3
"""Add a baseline through individual PF resource APIs; never use bulk import."""
import argparse
import base64
import datetime
import json
import hashlib
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch
import federate as pf
from federate_config import candidate_forward
from federate_recovery import prepare, PROTECTED


def requests_for(payload, current, schema, resume=False):
    existing = {op['resourceType']: op.get('items', []) for op in current['operations']}
    if not resume and (existing.get('/idp/adapters') or existing.get('/authenticationPolicyContracts')):
        raise ValueError('Identity objects already exist; refusing baseline recovery')
    result = []
    for op in payload['operations']:
        resource = op['resourceType']
        if resource in PROTECTED: raise ValueError('Protected resource in recovery')
        methods = schema['paths'].get(resource, {})
        for item in op.get('items', []):
            if resource in ('/certificates/ca', '/keyPairs/signing'):
                if any(old.get('id') == item.get('id') for old in existing.get(resource, [])): continue
                method, path = 'POST', resource+'/import'
            elif 'post' in methods and item.get('id'):
                if any(old.get('id') == item['id'] for old in existing.get(resource, [])): continue
                method, path = 'POST', resource
            elif 'put' in methods:
                method, path = 'PUT', resource
            else:
                raise ValueError('Unsupported recovery operation: '+resource)
            if method.lower() not in schema['paths'].get(path, {}):
                raise ValueError('API does not support '+method+' '+path)
            result.append((method, path, item))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('blue','green'), required=True)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--username', default='Administrator')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--maintenance', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    with zipfile.ZipFile(args.backup) as archive:
        if archive.testzip() or 'pf.jwk' not in archive.namelist():
            raise ValueError('Require a valid PF configuration archive including the master key')
    project = pf.project
    project.check_context(project.config())
    if args.apply and pf.observed_colour() == args.target and not args.maintenance:
        raise ValueError('Active colour requires an explicit maintenance window')
    folder = pf.SECRET/'recovery'; folder.mkdir(exist_ok=True)
    cert = subprocess.check_output(['kubectl','-n',pf.namespace(args.target),'exec',
        f'pf-{args.target}-pingfederate-admin-0','-c','pingfederate-admin','--',
        'keytool','-printcert','-sslserver','localhost:9999','-rfc'],text=True)
    cert_path = folder/f'{args.target}-admin-live.crt'; cert_path.write_text(cert)
    context = ssl.create_default_context(cafile=str(cert_path))
    token = base64.b64encode((args.username+':'+(pf.SECRET/'federate-admin-password').read_text().strip()).encode()).decode()
    lookup = socket.getaddrinfo
    # The forward binds IPv4 only. Avoid unrelated existing IPv6 localhost forwards,
    # while preserving TLS hostname verification for localhost.
    with candidate_forward(args.target) as url, patch('socket.getaddrinfo',
            side_effect=lambda host,*a,**kw: lookup('127.0.0.1' if host=='localhost' else host,*a,**kw)):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context))
        def api(method, path, body=None):
            request = urllib.request.Request(url+'/pf-admin-api/v1'+path,
                data=None if body is None else json.dumps(body).encode(), method=method,
                headers={'Authorization':'Basic '+token,'X-XSRF-Header':'PingFederate',
                         'Content-Type':'application/json'})
            try:
                with opener.open(request, timeout=90) as response:
                    data = response.read()
                    return json.loads(data) if data else None
            except urllib.error.HTTPError as error:
                path_out = folder/f'{args.target}-last-error.json'
                path_out.write_bytes(error.read()); path_out.chmod(0o600)
                raise ValueError(f'{method} {path}: HTTP {error.code}; private details in {path_out}') from None
        current = api('GET','/bulk/export')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = folder/f'{args.target}-before-repair-{stamp}.json'
        backup.write_text(json.dumps(current)); backup.chmod(0o600)
        schema = api('GET','/openapi.json')
        payload = prepare(args.target)
        digest = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        journal_path = folder/f'{args.target}-repair-journal.json'
        if args.resume:
            journal = json.loads(journal_path.read_text())
            if journal['payload_sha256'] != digest: raise ValueError('Recovery payload changed; review before resuming')
        else:
            journal = {'payload_sha256':digest,'completed':[]}
        changes = requests_for(payload, current, schema, resume=args.resume)
        changes = [(method,path,item) for method,path,item in changes
                   if [method,path,item.get('id','')] not in journal['completed']]
        for method,path,item in changes: print(method,path,item.get('id',''))
        print(f'{len(changes)} individual API operations; current export saved privately.')
        if not args.apply: return
        for method,path,item in changes:
            api(method,path,item)
            journal['completed'].append([method,path,item.get('id','')])
            journal_path.write_text(json.dumps(journal)); journal_path.chmod(0o600)
            print('Completed:',method,path,item.get('id',''))
        after = api('GET','/bulk/export')
        (folder/f'{args.target}-after-repair-{stamp}.json').write_text(json.dumps(after))
        print('Resource recovery completed. Configuration has NOT been replicated to engines.')


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error: raise SystemExit(str(error))
