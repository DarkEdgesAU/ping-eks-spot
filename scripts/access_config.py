#!/usr/bin/env python3
"""Configure PingAccess through a certificate-verified Kubernetes tunnel."""
import base64
import argparse
import datetime as dt
import contextlib
import json
import re
import socket
import ssl
import subprocess
import time
import urllib.request
from unittest.mock import patch
import access


@contextlib.contextmanager
def admin(colour, product='pa'):
    pf = product == 'pf'
    engine = product == 'pa-engine'
    host = f'pf-{colour}-pingfederate-admin' if pf else f'pa-{colour}-pingaccess-admin'
    pod = host + '-0'
    ns = access.federate.namespace(colour) if pf else access.namespace(colour)
    if engine:
        pods = json.loads(subprocess.check_output(['kubectl', '-n', ns, 'get', 'pods',
            '-l', 'app.kubernetes.io/name=pingaccess-engine', '-o', 'json']))['items']
        pod = next(x['metadata']['name'] for x in pods if any(
            v['type'] == 'Ready' and v['status'] == 'True' for v in x['status'].get('conditions', [])))
    folder = access.ROOT / '.secrets' / f'{"pf" if pf else "pa"}-{colour}'
    folder.mkdir(exist_ok=True)
    remote_port = 9999 if pf else (3000 if engine else 9000)
    cert = folder / ('engine-live.crt' if engine else 'admin-live.crt')
    cert.write_bytes(subprocess.check_output(['kubectl', '-n', ns, 'exec', pod,
        '-c', 'pingfederate-admin' if pf else ('pingaccess-engine' if engine else 'pingaccess-admin'), '--', 'keytool', '-printcert',
        '-sslserver', f'localhost:{remote_port}', '-rfc']))
    port = (19999 if colour == 'blue' else 29999) if pf else (39000 if colour == 'blue' else 49000)
    if engine:
        port += 1
    access.project.BUILD.mkdir(exist_ok=True)
    log = access.project.BUILD / f'{product}-{colour}-api-forward.log'
    with log.open('w') as stream:
        process = subprocess.Popen(['kubectl', '-n', ns, 'port-forward', '--address', '127.0.0.1',
            pod, f'{port}:{remote_port}'], stdout=stream, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while 'Forwarding from' not in log.read_text():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise ValueError('PingAccess admin tunnel failed; check ' + str(log))
                time.sleep(.2)
            password_file = access.ROOT / '.secrets' / 'federate-admin-password' if pf else folder / 'admin-password'
            if pf and (folder / 'admin-password').is_file():
                password_file = folder / 'admin-password'
            password = password_file.read_text().strip()
            token = base64.b64encode((('Administrator' if pf else 'administrator') + ':' + password).encode()).decode()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))
            lookup = socket.getaddrinfo
            hostname = 'localhost' if pf else host
            def request(path, data=None, method='GET', raw=False):
                req = urllib.request.Request(f'https://{hostname}:{port}' + path,
                    data=None if data is None else json.dumps(data).encode(), method=method,
                    headers={'Authorization': 'Basic ' + token, 'X-Xsrf-Header': 'PingFederate' if pf else 'PingAccess',
                             'Content-Type': 'application/json'})
                if engine:
                    req.remove_header('Authorization')
                    req.add_header('Host', access.config()['engine_hostname'])
                with patch('socket.getaddrinfo', side_effect=lambda name, *a, **k:
                           lookup('127.0.0.1' if name == hostname else name, *a, **k)):
                    with opener.open(req, timeout=30) as response:
                        body = response.read()
                        return body.decode() if raw else (json.loads(body) if body else None)
            yield request
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def runtime(colour, host, context_root, virtual_host_id, trust_group_id):
    if colour not in access.project.COLOURS:
        raise ValueError('Invalid colour')
    if not context_root.startswith('/') or context_root.endswith('/') or '?' in context_root or '#' in context_root:
        raise ValueError('Use a context root such as /identity without a trailing slash')
    return {'targets': [f'pf-{colour}-pingfederate-engine.identity-federate-{colour}.svc.cluster.local:9031'],
            'host': host, 'port': 443, 'secure': True, 'backChannelSecure': True,
            'skipHostnameVerification': False, 'expectedHostname': 'localhost',
            'trustedCertificateGroupId': trust_group_id, 'auditLevel': 'ON', 'useProxy': False,
            'application': {'contextRoot': context_root, 'primaryVirtualHostId': virtual_host_id,
                            'additionalVirtualHostIds': [], 'policy': [], 'caseSensitive': True}}


def identity_rule(context_root):
    return {'name': 'Identity PF context rewrite',
            'className': 'com.pingidentity.pa.policy.rewrite.SynapseURLRewritePolicyInterceptor',
            'configuration': {'mapFrom': '^' + re.escape(context_root) + '/(.*)', 'mapTo': '/$1'}}


def configure(colour, apply=False):
    c = access.config()
    root = c.get('identity_context_root', '/identity')
    public_url = 'https://' + c['engine_hostname'] + root
    desired = runtime(colour, c['engine_hostname'], root, 0, 0)
    access.project.check_context(access.project.config())
    with admin(colour, 'pf') as pf, admin(colour) as pa:
        settings = pf('/pf-admin-api/v1/serverSettings')
        current = pa('/pa-admin-api/v3/pingfederate')
        print(f'{colour}: PF base URL {settings["federationInfo"]["baseUrl"]} -> {public_url}')
        print(json.dumps(desired, indent=2))
        print('Request policy:', json.dumps(identity_rule(root)))
        if not apply:
            print('Plan only; no API changes sent. Apply with --apply.')
            return
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = access.ROOT / '.secrets' / 'identity-backups' / f'{colour}-{stamp}'
        backup.mkdir(parents=True)
        (backup / 'pa.json').write_text(json.dumps(pa('/pa-admin-api/v3/config/export')))
        (backup / 'pf.json').write_text(json.dumps(pf('/pf-admin-api/v1/bulk/export')))
        (backup / 'pf-server-settings.json').write_text(json.dumps(settings))
        (backup / 'pa-runtime.json').write_text(json.dumps(current))
        # Obtain the actual runtime certificate through authenticated Kubernetes,
        # not an unauthenticated network trust-on-first-use connection.
        ns = access.federate.namespace(colour)
        pods = json.loads(subprocess.check_output(['kubectl', '-n', ns, 'get', 'pods',
            '-l', 'app.kubernetes.io/name=pingfederate-engine', '-o', 'json']))['items']
        certificates = set()
        for pod in pods:
            pem = subprocess.check_output(['kubectl', '-n', ns, 'exec', pod['metadata']['name'],
                '-c', 'pingfederate-engine', '--', 'keytool', '-printcert', '-sslserver', 'localhost:9031', '-rfc'], text=True)
            certificates.add(pem.strip())
        if len(certificates) != 1:
            raise ValueError('PF engines must present the same verified runtime certificate')
        pem = certificates.pop()
        cert_data = base64.b64encode(pem.encode()).decode()
        alias = f'identity-pf-{colour}'
        found = [x for x in pa('/pa-admin-api/v3/certificates')['items'] if x['alias'] == alias]
        endpoint = '/pa-admin-api/v3/certificates' + (f'/{found[0]["id"]}' if found else '')
        cert = pa(endpoint, {'alias': alias, 'fileData': cert_data}, 'PUT' if found else 'POST')
        group_name = f'Identity PF {colour}'
        found = [x for x in pa('/pa-admin-api/v3/trustedCertificateGroups')['items'] if x['name'] == group_name]
        endpoint = '/pa-admin-api/v3/trustedCertificateGroups' + (f'/{found[0]["id"]}' if found else '')
        group = pa(endpoint, {'name': group_name, 'useJavaTrustStore': False,
            'skipCertificateDateCheck': False, 'certIds': [cert['id']]}, 'PUT' if found else 'POST')
        found = [x for x in pa('/pa-admin-api/v3/virtualhosts')['items']
                 if x['host'] == c['engine_hostname'] and x['port'] == 443]
        vh = found[0] if found else pa('/pa-admin-api/v3/virtualhosts',
            {'host': c['engine_hostname'], 'port': 443}, 'POST')
        desired = runtime(colour, c['engine_hostname'], root, vh['id'], group['id'])
        rule_name = 'Identity PF context rewrite'
        found = [x for x in pa('/pa-admin-api/v3/rules')['items'] if x['name'] == rule_name]
        endpoint = '/pa-admin-api/v3/rules' + (f'/{found[0]["id"]}' if found else '')
        rule = pa(endpoint, identity_rule(root),
            'PUT' if found else 'POST')
        desired['application']['policy'] = [{'type': 'Rule', 'id': rule['id']}]
        pa('/pa-admin-api/v3/pingfederate', desired, 'PUT')
        # Preserve existing federation identifiers and unrelated server settings.
        settings['federationInfo']['baseUrl'] = public_url
        pf('/pf-admin-api/v1/serverSettings', settings, 'PUT')
        pf('/pf-admin-api/v1/cluster/replicate', {}, 'POST')
        print(f'Configured {colour}: {public_url}/; private backup: {backup}')


def verify(colour):
    root = access.config().get('identity_context_root', '/identity')
    with admin(colour, 'pa-engine') as request:
        request(root + '/pf/heartbeat.ping', raw=True)
        print(f'{colour}: proxied PF heartbeat HTTP 200')
        metadata = request(root + '/.well-known/openid-configuration')
        public_url = 'https://' + access.config()['engine_hostname'] + root
        for key in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            if not metadata[key].startswith(public_url + '/'):
                raise ValueError(f'{key} does not use the public identity URL')
        print(json.dumps({k: metadata[k] for k in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri')}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=access.project.COLOURS, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        verify(args.target)
    else:
        configure(args.target, args.apply)
