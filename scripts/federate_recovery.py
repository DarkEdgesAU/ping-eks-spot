#!/usr/bin/env python3
"""Prepare a secret-bearing baseline recovery payload; never deploy or import it."""
import argparse
import json
import re
from pathlib import Path
import federate as pf
import project

# Preserve wizard-created access, licensing and encryption/transport identity.
PROTECTED = {'/administrativeAccounts', '/license', '/license/agreement',
             '/serverSettings/systemKeys', '/keyPairs/sslServer',
             '/keyPairs/sslServer/settings', '/configStore'}


def prepare(colour):
    config = pf.config()
    template = json.loads((pf.ROOT/'profiles/pingfederate/instance/bulk-config/data.json.subst').read_text())
    template['operations'] = [op for op in template['operations']
                              if op['resourceType'] not in PROTECTED]
    values = {file.name: file.read_text().strip()
              for file in (pf.SECRET/f'pf-{colour}'/'vars').iterdir() if file.is_file()}
    values.update(PF_ADMIN_PUBLIC_HOSTNAME=pf.admin_hostname(config,colour),
                  USER_BASE_DN=project.config()['base_dn'],
                  PF_ENGINE_PRIVATE_HOSTNAME=f'pf-{colour}-pingfederate-engine.{pf.namespace(colour)}.svc.cluster.local',
                  PF_ENGINE_PRIVATE_PORT_HTTPS='9031',
                  PF_ADMIN_PUBLIC_PORT_HTTPS='443',
                  PF_ENGINE_PUBLIC_HOSTNAME=config['engine_hostname'],
                  PF_ENGINE_PUBLIC_PORT_HTTPS='443',
                  PF_ENGINE_BASE_URL=config['engine_hostname']+':443',
                  PD_ENGINE_PRIVATE_HOSTNAME='pd-pingdirectoryproxy.identity-proxy.svc.cluster.local',
                  PD_ENGINE_PRIVATE_PORT_LDAP='636',
                  PD_DELEGATOR_URL=config['engine_hostname']+':443')
    def expand(value):
        if isinstance(value, dict):
            result = {key: expand(item) for key, item in value.items()}
            if 'allowedOrigins' in result:
                result['allowedOrigins'] = list(dict.fromkeys(result['allowedOrigins']))
            return result
        if isinstance(value, list): return [expand(item) for item in value]
        if isinstance(value, str):
            def replace(match):
                if match[1] in ('/', 'mail', 'username', 'pf.server.data.dir'): return match[0]
                if match[1] not in values: raise ValueError('Missing substitution: '+match[1])
                return values[match[1]]
            return re.sub(r'\$\{([^}]+)\}', replace, value)
        return value
    payload = expand(template)
    for operation in payload['operations']:
        if operation['resourceType'] == '/serverSettings':
            info = operation['items'][0]['federationInfo']
            info.update(baseUrl='https://'+config['engine_hostname'],
                        saml2EntityId='https://'+config['engine_hostname'],
                        saml1xIssuerId=config['engine_hostname'])
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('blue', 'green'), required=True)
    args = parser.parse_args()
    payload = prepare(args.target)
    folder = pf.SECRET/'recovery'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder/f'pf-{args.target}-baseline.json'
    path.write_text(json.dumps(payload, indent=2)+'\n')
    path.chmod(0o600)
    print(f'Prepared {path}; contains secrets. No API requests sent.')
    print('Accounts, licenses, system keys, SSL keys/settings and configStore are excluded.')
    print('DO NOT bulk-import this draft: bulk import can clear existing configuration. Recovery must use individual resource APIs.')


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError) as error: raise SystemExit(str(error))
