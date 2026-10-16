#!/usr/bin/env python3
"""Prepare OAuth seed credentials locally with the selected PF image; no AWS calls."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import project

SOURCE = project.ROOT/'profiles/pingdirectory/oauth-clients.json'
PRIVATE = project.ROOT/'.secrets/oauth-clients'


def mapping():
    return json.loads(SOURCE.read_text())


def runtime_files():
    c = mapping()
    return [item['file'] for item in c['hashes']+c['jwks']+c.get('reversible', [])]


def prepare(image=None):
    import federate
    image = image or 'pingidentity/pingfederate:'+federate.config()['base_image_tag']
    if ':' not in image or image.endswith((':latest', ':edge')):
        raise ValueError('Use a pinned PF image matching the deployed product version')
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)
    c = mapping()
    for item in c['hashes']:
        path = PRIVATE/(item['alias']+'.secret')
        if not path.exists(): path.write_text(secrets.token_urlsafe(48))
    (PRIVATE/'hash-plan.tsv').write_text('\n'.join(x['alias'] for x in c['hashes'])+'\n')
    (PRIVATE/'jwk-plan.tsv').write_text(''.join('\t'.join([x['alias'], k['kty'], k['kid'], k.get('use', '')])+'\n'
        for x in c['jwks'] for k in x['keys']))
    project.run(['docker', 'run', '--rm', '--network', 'none', '--hostname', 'oauth-seed',
        '--add-host', 'oauth-seed:127.0.0.1', '--user', str(os.getuid())+':0' if hasattr(os, 'getuid') else '0:0',
        '--entrypoint', 'java',
        '-v', str(PRIVATE)+':/credentials',
        '-v', str(project.ROOT/'scripts/OAuthSeedCredentials.java')+':/OAuthSeedCredentials.java:ro',
        image, '--add-opens=java.base/java.lang=ALL-UNNAMED', '--add-opens=java.base/java.util=ALL-UNNAMED',
        '-Dpf.home=/opt/server', '--class-path', '/opt/server/server/default/lib/*', '/OAuthSeedCredentials.java'])
    for path in PRIVATE.iterdir(): path.chmod(0o600)
    for name in runtime_files():
        if not (PRIVATE/name).is_file(): raise ValueError('Missing generated OAuth credential: '+name)
    print('Prepared OAuth credentials under .secrets/oauth-clients. No cluster changes; existing credentials preserved.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image')
    a = p.parse_args()
    try: prepare(a.image)
    except (ValueError, OSError, subprocess.CalledProcessError) as e: raise SystemExit('Error: '+str(e))
