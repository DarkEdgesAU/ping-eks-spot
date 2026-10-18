#!/usr/bin/env python3
"""Small authenticated OpenSearch log-shipping POC; no cloud calls during render."""
import argparse
import base64
import copy
import json
from pathlib import Path
import secrets as random
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'observability'
PRIVATE = ROOT / '.secrets/logging'
NS = 'logging'


def config():
    path = ROOT / 'observability.json'
    c = json.loads((path if path.exists() else SOURCE/'config.example.json').read_text())
    if c['namespace'] != NS:
        raise ValueError('The POC uses the logging namespace')
    for key in ('opensearch_image', 'dashboards_image', 'collector_image'):
        if c[key].endswith(':latest') or ':' not in c[key]:
            raise ValueError('Pin logging image versions')
    return c


def collector_config():
    return {name: (SOURCE/name).read_text() for name in ('sidecar.conf', 'parsers.conf')}


def attach(documents, enabled=None, namespace=None):
    c = config()
    if not (c['enabled'] if enabled is None else enabled):
        return documents
    result = copy.deepcopy(documents)
    namespaces = set()
    for doc in result:
        if not doc or doc['kind'] not in ('Deployment', 'StatefulSet'):
            continue
        main = [x for x in doc['spec']['template']['spec']['containers'] if x['name'].startswith(('pingdirectory', 'pingfederate', 'pingaccess'))]
        if not main:
            continue
        if len(main) != 1:
            raise ValueError('Ambiguous Ping product container')
        main = main[0]
        ns = doc['metadata'].get('namespace', namespace)
        if not ns:
            raise ValueError('Logging attachment requires the workload namespace')
        doc['metadata']['namespace'] = ns
        namespaces.add(ns)
        pod = doc['spec']['template']['spec']
        pod.setdefault('securityContext', {}).setdefault('fsGroup', 0)
        if any(x['name'] == 'log-shipper' for x in pod['containers']):
            continue
        mounts = main.setdefault('volumeMounts', [])
        out = next((x for x in mounts if x['mountPath'] == '/opt/out'), None)
        if out is None:
            pod.setdefault('volumes', []).append({'name': 'logging-out', 'emptyDir': {}})
            out = {'name': 'logging-out', 'mountPath': '/opt/out'}
            mounts.append(out)
        pod.setdefault('volumes', []).extend([
            {'name': 'logging-config', 'configMap': {'name': 'ping-log-collector'}},
            {'name': 'logging-credentials', 'secret': {'secretName': 'log-shipper'}},
            {'name': 'logging-buffer', 'emptyDir': {'sizeLimit': '256Mi'}}])
        pod['containers'].append({'name': 'log-shipper', 'image': c['collector_image'],
            'args': ['-c', '/fluent-bit/etc/poc/sidecar.conf'],
            'resources': {'requests': {'cpu': '25m', 'memory': '64Mi'}, 'limits': {'memory': '192Mi'}},
            'securityContext': {'runAsUser': 9031, 'runAsGroup': 0, 'allowPrivilegeEscalation': False,
                                'capabilities': {'drop': ['ALL']}},
            'env': [{'name': key, 'valueFrom': {'fieldRef': {'fieldPath': field}}}
                    for key, field in [('POD_NAME', 'metadata.name'), ('POD_NAMESPACE', 'metadata.namespace')]] + [
                {'name': 'PRODUCT', 'value': main['name']},
                {'name': 'LOG_PATHS', 'value': '/logs/instance/logs/*' if main['name'].startswith('pingdirectory') else '/logs/instance/log/*.log,/logs/instance/log/*.json'},
                {'name': 'COLOUR', 'value': ns.rsplit('-', 1)[-1]},
                {'name': 'LOG_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'log-shipper', 'key': 'password'}}}],
            'volumeMounts': [{'name': out['name'], 'mountPath': '/logs', 'readOnly': True},
                {'name': 'logging-config', 'mountPath': '/fluent-bit/etc/poc', 'readOnly': True},
                {'name': 'logging-credentials', 'mountPath': '/credentials', 'readOnly': True},
                {'name': 'logging-buffer', 'mountPath': '/buffers'}]})
    for ns in sorted(namespaces):
        result.append({'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': 'ping-log-collector', 'namespace': ns}, 'data': collector_config()})
    return result


def resource(kind, name, spec=None, **extra):
    obj = {'apiVersion': 'apps/v1' if kind in ('StatefulSet', 'Deployment', 'DaemonSet') else 'v1',
           'kind': kind, 'metadata': {'name': name, 'namespace': NS}, **extra}
    if spec is not None:
        obj['spec'] = spec
    return obj


def workload(kind, name, pod):
    return resource(kind, name, {'selector': {'matchLabels': {'app': name}},
        'template': {'metadata': {'labels': {'app': name}}, 'spec': pod},
        **({} if kind == 'DaemonSet' else {'replicas': 1})})


def manifests():
    c = config()
    cfg = {n: (SOURCE/n).read_text() for n in ('opensearch.yml', 'prepare-config.sh', 'dashboards.yml')}
    docs = [resource('ConfigMap', 'logging-config', data=cfg)]
    search = workload('StatefulSet', 'opensearch', {
        'automountServiceAccountToken': False,
        'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000},
        'initContainers': [{'name': 'prepare-config', 'image': c['opensearch_image'],
            'command': ['sh', '/poc/prepare-config.sh'], 'volumeMounts': [
                {'name': 'config', 'mountPath': '/work'}, {'name': 'settings', 'mountPath': '/poc'},
                {'name': 'auth', 'mountPath': '/auth'}]}],
        'containers': [{'name': 'opensearch', 'image': c['opensearch_image'],
            'env': [{'name': 'DISABLE_INSTALL_DEMO_CONFIG', 'value': 'true'},
                    {'name': 'OPENSEARCH_JAVA_OPTS', 'value': '-Xms512m -Xmx512m'}],
            'ports': [{'containerPort': 9200}],
            'resources': {'requests': {'cpu': '500m', 'memory': '1536Mi'}, 'limits': {'memory': '3Gi'}},
            'readinessProbe': {'tcpSocket': {'port': 9200}, 'initialDelaySeconds': 20},
            'volumeMounts': [{'name': 'config', 'mountPath': '/usr/share/opensearch/config'},
                {'name': 'tls', 'mountPath': '/usr/share/opensearch/config/tls', 'readOnly': True},
                {'name': 'data', 'mountPath': '/usr/share/opensearch/data'}]}],
        'volumes': [{'name': 'config', 'emptyDir': {}},
            {'name': 'settings', 'configMap': {'name': 'logging-config'}},
            {'name': 'auth', 'secret': {'secretName': 'logging-auth'}},
            {'name': 'tls', 'secret': {'secretName': 'logging-tls'}}]})
    search['spec'].update({'serviceName': 'opensearch',
        'persistentVolumeClaimRetentionPolicy': {'whenDeleted': 'Retain', 'whenScaled': 'Retain'},
        'volumeClaimTemplates': [{'metadata': {'name': 'data'}, 'spec': {'accessModes': ['ReadWriteOnce'],
            'storageClassName': 'pingdirectory-ebs', 'resources': {'requests': {'storage': c['storage']}}}}]})
    search['metadata']['annotations'] = {'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'}
    docs.append(search)
    docs.append(workload('Deployment', 'dashboards', {'automountServiceAccountToken': False,
        'containers': [{'name': 'dashboards', 'image': c['dashboards_image'],
            'env': [{'name': 'DASHBOARDS_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'logging-auth', 'key': 'dashboards-password'}}}],
            'ports': [{'containerPort': 5601}],
            'resources': {'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'memory': '1536Mi'}},
            'readinessProbe': {'httpGet': {'path': '/app/login', 'port': 5601, 'scheme': 'HTTPS'}, 'initialDelaySeconds': 30},
            'volumeMounts': [{'name': 'settings', 'mountPath': '/usr/share/opensearch-dashboards/config/opensearch_dashboards.yml', 'subPath': 'dashboards.yml'},
                {'name': 'tls', 'mountPath': '/tls', 'readOnly': True}]}],
        'volumes': [{'name': 'settings', 'configMap': {'name': 'logging-config'}},
                    {'name': 'tls', 'secret': {'secretName': 'logging-tls'}}]}))
    for name, port in [('opensearch', 9200), ('dashboards', 5601)]:
        docs.append(resource('Service', name, {'selector': {'app': name}, 'ports': [{'port': port, 'targetPort': port}]}))
    node_cfg = (SOURCE/'sidecar.conf').read_text()
    start, end = node_cfg.index('[INPUT]'), node_cfg.index('[OUTPUT]')
    node_cfg = node_cfg[:start] + '''[INPUT]
    Name tail
    Path /var/log/containers/*.log
    Tag kube.containers
    Parser cri
    Path_Key source_file
    DB /buffers/node.db
    Read_from_Head Off
    Skip_Long_Lines On
    storage.type filesystem
[FILTER]
    Name parser
    Match *
    Key_Name source_file
    Parser kube_path
    Reserve_Data On
    Preserve_Key On

[FILTER]
    Name modify
    Match *
    Add stream container
    Add node ${NODE_NAME}

''' + node_cfg[end:].replace('logs-ping', 'logs-containers')
    docs.append(resource('ConfigMap', 'node-log-collector', data={'sidecar.conf': node_cfg, 'parsers.conf': (SOURCE/'parsers.conf').read_text()}))
    docs.append(workload('DaemonSet', 'node-log-collector', {'automountServiceAccountToken': False,
        'tolerations': [{'operator': 'Exists'}],
        'containers': [{'name': 'collector', 'image': c['collector_image'], 'args': ['-c', '/fluent-bit/etc/poc/sidecar.conf'],
            'securityContext': {'runAsUser': 0, 'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
            'env': [{'name': 'NODE_NAME', 'valueFrom': {'fieldRef': {'fieldPath': 'spec.nodeName'}}},
                {'name': 'LOG_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'log-shipper', 'key': 'password'}}}],
            'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'memory': '192Mi'}},
            'volumeMounts': [{'name': 'logs', 'mountPath': '/var/log', 'readOnly': True},
                {'name': 'cfg', 'mountPath': '/fluent-bit/etc/poc', 'readOnly': True},
                {'name': 'auth', 'mountPath': '/credentials', 'readOnly': True}, {'name': 'buffer', 'mountPath': '/buffers'}]}],
        'volumes': [{'name': 'logs', 'hostPath': {'path': '/var/log', 'type': 'Directory'}},
            {'name': 'cfg', 'configMap': {'name': 'node-log-collector'}},
            {'name': 'auth', 'secret': {'secretName': 'log-shipper'}}, {'name': 'buffer', 'emptyDir': {'sizeLimit': '256Mi'}}]}))
    return docs


def prepare_secrets():
    from vault_runtime import generator_guard
    generator_guard()
    import bcrypt
    PRIVATE.mkdir(parents=True, exist_ok=True)
    ca = ROOT/'.secrets'
    if not (ca/'ca.key').exists() or not (ca/'ca.crt').exists():
        raise ValueError('Existing internal CA is required; do not regenerate it')
    for name in ('admin', 'shipper', 'dashboards'):
        path = PRIVATE/(name+'-password')
        if not path.exists():
            path.write_text('Poc!'+random.token_urlsafe(30))
    users = {'_meta': {'type': 'internalusers', 'config_version': 2}}
    for username, name, roles in [('admin', 'admin', ['admin']), ('logshipper', 'shipper', ['log_writer']), ('kibanaserver', 'dashboards', ['kibana_server'])]:
        users[username] = {'hash': bcrypt.hashpw((PRIVATE/(name+'-password')).read_bytes(), bcrypt.gensalt()).decode(),
                           'reserved': True, 'backend_roles': roles}
    (PRIVATE/'internal_users.yml').write_text(yaml.safe_dump(users))
    for name, names in [('opensearch', ['opensearch', 'localhost', 'opensearch.logging.svc', 'opensearch.logging.svc.cluster.local']),
                        ('dashboards', ['dashboards', 'localhost', config()['hostname'], 'dashboards.logging.svc.cluster.local'])]:
        if (PRIVATE/(name+'.crt')).exists():
            continue
        ext = PRIVATE/(name+'.cnf')
        ext.write_text('subjectAltName='+','.join('DNS:'+x for x in names)+
            '\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment'+
            '\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nextendedKeyUsage=serverAuth,clientAuth\n')
        for args in [ ['openssl', 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN='+name,
                      '-keyout', str(PRIVATE/(name+'.key')), '-out', str(PRIVATE/(name+'.csr'))],
                     ['openssl', 'x509', '-req', '-in', str(PRIVATE/(name+'.csr')), '-CA', str(ca/'ca.crt'),
                      '-CAkey', str(ca/'ca.key'), '-CAcreateserial', '-days', '365', '-extfile', str(ext), '-out', str(PRIVATE/(name+'.crt'))] ]:
            subprocess.run(args, check=True, capture_output=True)
    # The existing development CA lacks keyUsage. Reissue only the logging
    # trust certificate using the SAME subject, key and serial; do not replace
    # the shared CA or any other product's trust store.
    ext=PRIVATE/'ca-extensions.cnf'
    ext.write_text('basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
    subject=subprocess.check_output(['openssl','x509','-in',str(ca/'ca.crt'),'-noout','-subject','-nameopt','compat'],text=True).strip().split('=',1)[1].strip()
    serial=subprocess.check_output(['openssl','x509','-in',str(ca/'ca.crt'),'-noout','-serial'],text=True).strip().split('=',1)[1]
    subprocess.run(['openssl','req','-new','-key',str(ca/'ca.key'),'-subj',subject,
        '-out',str(PRIVATE/'ca.csr')],check=True,capture_output=True)
    subprocess.run(['openssl','x509','-req','-in',str(PRIVATE/'ca.csr'),'-signkey',str(ca/'ca.key'),
        '-set_serial','0x'+serial,'-extfile',str(ext),'-days','3650','-out',str(PRIVATE/'ca.crt')],check=True,capture_output=True)
    print('Prepared private logging credentials and CA-issued certificates; no cluster changes.')


def export():
    folder = ROOT/'gitops/workloads/logging'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'resources.yaml').write_text(yaml.safe_dump_all(manifests(), sort_keys=False), newline='\n')


def cluster_secrets():
    from vault_runtime import generator_guard
    generator_guard()
    import project
    project.check_context(project.config())
    prepare_secrets()
    namespaces = [NS, 'identity-blue', 'identity-green', 'identity-proxy', 'identity-federate-blue',
                  'identity-federate-green', 'identity-access-blue', 'identity-access-green']
    for ns in namespaces:
        project.apply(resource('Namespace', ns) | {'metadata': {'name': ns}})
        project.apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'log-shipper', 'namespace': ns},
            'data': {'password': base64.b64encode((PRIVATE/'shipper-password').read_bytes()).decode(),
                     'ca.crt': base64.b64encode((PRIVATE/'ca.crt').read_bytes()).decode()}})
    for name, files in [('logging-auth', {'internal_users.yml': 'internal_users.yml', 'dashboards-password': 'dashboards-password'}),
                        ('logging-tls', {n: n for n in ('opensearch.crt', 'opensearch.key', 'dashboards.crt', 'dashboards.key', 'ca.crt')})]:
        project.apply(resource('Secret', name, data={k: base64.b64encode((PRIVATE/v).read_bytes()).decode() for k,v in files.items()}))


def ingress():
    import gitops
    c = gitops.config()
    import project
    project.check_context(project.config())
    obj = resource('Ingress', 'logs', {'ingressClassName': 'alb', 'rules': [{'host': config()['hostname'],
        'http': {'paths': [{'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': 'dashboards', 'port': {'number': 5601}}}}]}}]})
    obj['apiVersion'] = 'networking.k8s.io/v1'
    if not c['origin_certificate_arn'] or not c['cloudflare_ipv4_cidrs']:
        raise ValueError('Configure origin certificate and Cloudflare source CIDRs in gitops/config.json')
    obj['metadata']['annotations'] = {
        'alb.ingress.kubernetes.io/scheme': 'internet-facing', 'alb.ingress.kubernetes.io/target-type': 'ip',
        'alb.ingress.kubernetes.io/listen-ports': '[{"HTTPS":443}]',
        'alb.ingress.kubernetes.io/certificate-arn': c['origin_certificate_arn'],
        'alb.ingress.kubernetes.io/inbound-cidrs': ','.join(c['cloudflare_ipv4_cidrs']),
        'alb.ingress.kubernetes.io/backend-protocol': 'HTTPS',
        'alb.ingress.kubernetes.io/healthcheck-protocol': 'HTTPS',
        'alb.ingress.kubernetes.io/healthcheck-path': '/app/login',
        'alb.ingress.kubernetes.io/success-codes': '200-399'}
    project.apply(obj)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'secrets', 'render', 'ingress', 'enable'])
    args = parser.parse_args()
    if args.command == 'prepare': prepare_secrets()
    elif args.command == 'secrets': cluster_secrets()
    elif args.command == 'render': export()
    elif args.command == 'ingress': ingress()
    else:
        c = config(); c['enabled'] = True
        (ROOT/'observability.json').write_text(json.dumps(c, indent=2)+'\n')
        print('Enabled logging in local renders. Run logging-secrets and review gitops-export before syncing.')
