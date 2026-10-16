#!/usr/bin/env python3
"""Render optional metrics, Delegator and PingCentral; cloud mutations are explicit."""
import argparse
import base64
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import urlparse
import yaml
import project

ROOT = project.ROOT
SOURCE = ROOT/'platform-services'
PRIVATE = ROOT/'.secrets/platform'
APPLICATIONS = {'metrics': 'monitoring', 'delegator': 'identity-delegator', 'pingcentral': 'identity-central'}
PING_NAMESPACES = ['identity-blue', 'identity-green', 'identity-proxy',
    'identity-federate-blue', 'identity-federate-green', 'identity-access-blue', 'identity-access-green']


def config():
    path = ROOT/'platform-services.json'
    c = json.loads((path if path.exists() else SOURCE/'config.example.json').read_text())
    for key in ('monitoring_hostname', 'delegator_hostname', 'central_hostname'):
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', c[key]):
            raise ValueError('Invalid platform hostname')
    if c['directory_colour'] not in ('blue', 'green'):
        raise ValueError('Invalid Directory colour')
    u = urlparse(c['pf_authority'])
    if u.scheme != 'https' or not u.hostname or u.username or u.query or u.fragment:
        raise ValueError('PF authority must be an HTTPS issuer URL')
    for key in ('central_image', 'delegator_image', 'prometheus_image', 'grafana_image', 'alertmanager_image', 'telegraf_image'):
        if ':' not in c[key] or ':latest' in c[key] or ':edge' in c[key]:
            raise ValueError('Pin platform images')
    return c


def obj(kind, name, ns, **kwargs):
    api = 'apps/v1' if kind in ('StatefulSet', 'Deployment') else 'v1'
    if kind in ('Role', 'RoleBinding'): api = 'rbac.authorization.k8s.io/v1'
    return {'apiVersion': api, 'kind': kind, 'metadata': {'name': name, 'namespace': ns}, **kwargs}


def service(name, ns, port):
    return obj('Service', name, ns, spec={'selector': {'app': name},
        'ports': [{'name': 'https', 'port': port, 'targetPort': port}]})


def workload(name, ns, container, volumes, uid=9031, storage=None, enabled=True):
    pod = {'automountServiceAccountToken': False,
        'securityContext': {'runAsUser': uid, 'runAsGroup': 0, 'fsGroup': 0},
        'containers': [container], 'volumes': volumes}
    spec = {'replicas': 1 if enabled else 0, 'selector': {'matchLabels': {'app': name}},
        'template': {'metadata': {'labels': {'app': name}}, 'spec': pod}}
    doc = obj('StatefulSet' if storage else 'Deployment', name, ns, spec=spec)
    if storage:
        spec.update(serviceName=name, persistentVolumeClaimRetentionPolicy={'whenDeleted': 'Retain', 'whenScaled': 'Retain'},
            volumeClaimTemplates=[{'metadata': {'name': 'data'}, 'spec': {'accessModes': ['ReadWriteOnce'],
                'storageClassName': 'pingdirectory-ebs', 'resources': {'requests': {'storage': storage}}}}])
        doc['metadata']['annotations'] = {'argocd.argoproj.io/sync-options': 'Prune=false,Delete=false'}
    return doc


def metrics_config():
    scrape = {'job_name': 'ping-pods', 'scheme': 'https',
        'tls_config': {'ca_file': '/tls/ca.crt', 'server_name': 'ping-metrics'},
        'kubernetes_sd_configs': [{'role': 'pod', 'namespaces': {'names': PING_NAMESPACES}}],
        'relabel_configs': [
            {'source_labels': ['__meta_kubernetes_pod_container_port_name'], 'regex': 'ping-metrics', 'action': 'keep'},
            {'source_labels': ['__meta_kubernetes_pod_phase'], 'regex': 'Running', 'action': 'keep'},
            *[{'source_labels': [source], 'target_label': target} for source, target in [
                ('__meta_kubernetes_namespace', 'namespace'), ('__meta_kubernetes_pod_name', 'pod')]] ]}
    return {'global': {'scrape_interval': '15s', 'evaluation_interval': '15s'},
        'rule_files': ['/settings/alerts.yml'], 'alerting': {'alertmanagers': [{'static_configs': [{'targets': ['alertmanager:9093']}]}]},
        'scrape_configs': [{'job_name': 'prometheus', 'static_configs': [{'targets': ['localhost:9090']} ]}, scrape]}


def alerts():
    rules = [
        ('PingExporterDown', 'up{job="ping-pods"} == 0', 'Ping metrics exporter is unreachable'),
        ('PingHealthFailed', 'http_response_result_code{job="ping-pods"} != 0 or http_response_http_response_code{job="ping-pods"} >= 400', 'Ping listener health check failed'),
        ('PingHealthSlow', 'http_response_response_time{job="ping-pods"} > 1', 'Ping listener health check exceeds one second'),
        ('PingTargetsMissing', 'absent(up{job="ping-pods"})', 'No Ping metrics targets discovered')]
    # Expected workloads: 6 PD, 3 proxy, 2 PF admins + 6 engines, 2 PA admins + 6 engines.
    rules.append(('PingTargetCountLow', 'count(up{job="ping-pods"}) < 25', 'Expected 25 Ping pods with metrics exporters'))
    return {'groups': [{'name': 'ping-poc', 'rules': [{'alert': name, 'expr': expr, 'for': '2m',
        'labels': {'severity': 'warning'}, 'annotations': {'summary': summary}} for name, expr, summary in rules]}]}


def metrics(c):
    ns = APPLICATIONS['metrics']
    datasource = {'apiVersion': 1, 'datasources': [{'name': 'Prometheus', 'uid': 'prometheus', 'type': 'prometheus',
        'access': 'proxy', 'url': 'http://prometheus:9090', 'isDefault': True, 'editable': False}]}
    panels = []
    for i, (title, expr) in enumerate([('Exporters up by namespace', 'sum by(namespace) (up{job="ping-pods"})'),
            ('Health response time', 'http_response_response_time{job="ping-pods"}'),
            ('Health HTTP status', 'http_response_http_response_code{job="ping-pods"}'),
            ('Firing alerts', 'ALERTS{alertstate="firing"}')]):
        panels.append({'id': i+1, 'title': title, 'type': 'timeseries', 'datasource': {'type': 'prometheus', 'uid': 'prometheus'},
            'gridPos': {'x': (i % 2)*12, 'y': (i//2)*8, 'w': 12, 'h': 8}, 'targets': [{'expr': expr, 'legendFormat': '{{namespace}} {{pod}}'}]})
    dashboard = {'uid': 'ping-poc', 'title': 'Ping platform POC', 'schemaVersion': 39, 'version': 1,
        'refresh': '15s', 'time': {'from': 'now-1h', 'to': 'now'}, 'panels': panels}
    data = {'prometheus.yml': yaml.safe_dump(metrics_config()), 'alerts.yml': yaml.safe_dump(alerts()),
        'alertmanager.yml': yaml.safe_dump({'route': {'receiver': 'local-only', 'group_by': ['alertname', 'namespace']},
            'receivers': [{'name': 'local-only'}]}), 'datasources.yml': yaml.safe_dump(datasource),
        'dashboards.yml': yaml.safe_dump({'apiVersion': 1, 'providers': [{'name': 'ping', 'options': {'path': '/dashboards'}}]}),
        'ping.json': json.dumps(dashboard)}
    docs = [obj('ConfigMap', 'metrics-config', ns, data=data), obj('ServiceAccount', 'prometheus', ns)]
    for target in PING_NAMESPACES:
        docs += [obj('Role', 'ping-metrics-discovery', target, rules=[{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']}]),
            obj('RoleBinding', 'ping-metrics-discovery', target,
                roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'ping-metrics-discovery'},
                subjects=[{'kind': 'ServiceAccount', 'name': 'prometheus', 'namespace': ns}])]
    settings = {'name': 'settings', 'configMap': {'name': 'metrics-config'}}
    for name, image, port, args in [
        ('prometheus', c['prometheus_image'], 9090, ['--config.file=/settings/prometheus.yml', '--storage.tsdb.path=/data', '--storage.tsdb.retention.time=1d']),
        ('alertmanager', c['alertmanager_image'], 9093, ['--config.file=/settings/alertmanager.yml', '--storage.path=/data', '--cluster.listen-address='])]:
        container = {'name': name, 'image': image, 'args': args, 'ports': [{'containerPort': port}],
            'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'memory': '1Gi'}},
            'volumeMounts': [{'name': 'settings', 'mountPath': '/settings'}, {'name': 'data', 'mountPath': '/data'}],
            'readinessProbe': {'httpGet': {'path': '/-/ready', 'port': port}}}
        vols = [settings]
        if name == 'prometheus':
            vols.append({'name': 'tls', 'secret': {'secretName': 'metrics-tls'}})
            container['volumeMounts'].append({'name': 'tls', 'mountPath': '/tls', 'readOnly': True})
        doc = workload(name, ns, container, vols, uid=65534, storage='5Gi', enabled=c['metrics_enabled'])
        if name == 'prometheus':
            doc['spec']['template']['spec'].update(serviceAccountName='prometheus', automountServiceAccountToken=True)
        docs += [doc, service(name, ns, port)]
    env = {'GF_SERVER_PROTOCOL': 'https', 'GF_SERVER_CERT_FILE': '/tls/tls.crt', 'GF_SERVER_CERT_KEY': '/tls/tls.key',
        'GF_SERVER_ROOT_URL': 'https://'+c['monitoring_hostname'], 'GF_AUTH_ANONYMOUS_ENABLED': 'false',
        'GF_USERS_ALLOW_SIGN_UP': 'false', 'GF_SECURITY_ADMIN_PASSWORD__FILE': '/auth/admin-password'}
    grafana = {'name': 'grafana', 'image': c['grafana_image'], 'ports': [{'containerPort': 3000}],
        'env': [{'name': k, 'value': v} for k,v in env.items()],
        'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'memory': '768Mi'}},
        'volumeMounts': [{'name': 'data', 'mountPath': '/var/lib/grafana'}, {'name': 'tls', 'mountPath': '/tls', 'readOnly': True},
            {'name': 'auth', 'mountPath': '/auth', 'readOnly': True},
            *[{'name': 'settings', 'mountPath': path, 'subPath': key} for key,path in [
                ('datasources.yml', '/etc/grafana/provisioning/datasources/ping.yml'),
                ('dashboards.yml', '/etc/grafana/provisioning/dashboards/ping.yml'), ('ping.json', '/dashboards/ping.json')]]],
        'readinessProbe': {'httpGet': {'path': '/api/health', 'port': 3000, 'scheme': 'HTTPS'}}}
    docs += [workload('grafana', ns, grafana, [settings, {'name': 'tls', 'secret': {'secretName': 'metrics-tls'}},
        {'name': 'auth', 'secret': {'secretName': 'grafana-auth'}}], uid=472, storage='2Gi', enabled=c['metrics_enabled']), service('grafana', ns, 3000)]
    return docs


def attach(documents, namespace=None, enabled=None):
    c = config()
    if not (c['metrics_enabled'] if enabled is None else enabled): return documents
    docs = copy.deepcopy(documents)
    namespaces = set()
    for doc in docs:
        if not doc or doc['kind'] not in ('StatefulSet', 'Deployment'): continue
        pod = doc['spec']['template']['spec']
        main = next((x for x in pod['containers'] if x['name'].startswith(('pingdirectory', 'pingfederate', 'pingaccess'))), None)
        if main is None or any(x['name'] == 'ping-metrics' for x in pod['containers']): continue
        ns = doc['metadata'].get('namespace', namespace)
        if ns not in PING_NAMESPACES: raise ValueError('Unexpected metrics namespace')
        namespaces.add(ns)
        name = main['name']
        url = 'https://localhost:1443/available-state' if name.startswith('pingdirectory') else (
            'https://localhost:9999/pf-admin-api/v1/version' if 'federate-admin' in name else
            'https://localhost:9031/pf/heartbeat.ping' if 'federate' in name else
            'https://localhost:9000/pa/heartbeat.ping' if 'access-admin' in name else 'https://localhost:3000/pa/heartbeat.ping')
        # Admin version endpoint requires authentication: probe the login page instead.
        if 'federate-admin' in name: url = 'https://localhost:9999/pingfederate/app'
        pod.setdefault('volumes', []).extend([{'name': 'metrics-config', 'configMap': {'name': 'ping-metrics'}},
            {'name': 'metrics-tls', 'secret': {'secretName': 'metrics-tls'}}])
        pod['containers'].append({'name': 'ping-metrics', 'image': c['telegraf_image'],
            'args': ['--config', '/settings/telegraf.conf'], 'env': [{'name': 'HEALTH_URL', 'value': url}],
            'ports': [{'name': 'ping-metrics', 'containerPort': 9273}],
            'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'memory': '128Mi'}},
            'securityContext': {'runAsUser': 9031, 'runAsGroup': 0, 'allowPrivilegeEscalation': False},
            'volumeMounts': [{'name': 'metrics-config', 'mountPath': '/settings', 'readOnly': True},
                {'name': 'metrics-tls', 'mountPath': '/tls', 'readOnly': True}]})
    for ns in sorted(namespaces):
        text = (SOURCE/'telegraf.conf').read_text()
        if ns.startswith(('identity-federate-', 'identity-access-')):
            product = 'pingfederate' if 'federate' in ns else 'pingaccess'
            port, path = (9031, '/pf/heartbeat.ping') if product == 'pingfederate' else (3000, '/pa/heartbeat.ping')
            # Engine heartbeats contain runtime statistics. Admins have no engine listener.
            # A separate config is selected on the container, so admins collect health only.
            engine = text + f'\n[[inputs.http]]\n  urls = ["https://localhost:{port}{path}"]\n  name_override = "{product}_heartbeat"\n  data_format = "json"\n  insecure_skip_verify = true\n'
            for doc in docs:
                if doc and doc['kind'] in ('Deployment', 'StatefulSet') and doc['metadata'].get('namespace', namespace) == ns:
                    containers = doc['spec']['template']['spec']['containers']
                    if any('engine' in x['name'] and x['name'].startswith(('pingfederate', 'pingaccess')) for x in containers):
                        next(x for x in containers if x['name'] == 'ping-metrics')['args'] = ['--config', '/settings/engine.conf']
            docs.append(obj('ConfigMap', 'ping-metrics', ns, data={'telegraf.conf': text, 'engine.conf': engine}))
        else:
            docs.append(obj('ConfigMap', 'ping-metrics', ns, data={'telegraf.conf': text}))
    return docs


def ping_workload(c, name):
    central = name == 'pingcentral'
    ns = APPLICATIONS[name]
    image = c['central_image' if central else 'delegator_image']
    port = 9022 if central else 6443
    env = {'PING_IDENTITY_ACCEPT_EULA': 'YES' if project.config()['accept_eula'] else 'NO',
        'VERBOSE': 'false', 'PING_DEBUG': 'false'}
    if central:
        env['PING_CENTRAL_BLIND_TRUST'] = 'false'
        env['PING_CENTRAL_VERIFY_HOSTNAME'] = 'true'
        data = {'instance/conf/application.properties': (SOURCE/'central.properties').read_text(),
            'hooks/07-apply-server-profile.sh.post': (SOURCE/'central-bootstrap.sh').read_text()}
    else:
        # PF 2609 supports the public issuer containing /identity, as verified in its JS.
        authority = c['pf_authority'].rstrip('/')
        config_js = "\n".join(['window.OIDC_AUTHORITY = '+json.dumps(authority)+';',
            "window.DADMIN_CLIENT_ID = 'dadmin';", "window.AUTHENTICATE_WITH_PKCE = true;",
            "window.DS_HOST = "+json.dumps(c['delegator_hostname'])+';', "window.DS_PORT = '';",
            "window.PD_DADMIN_CONFIG = { PROFILE_SCOPE_ENABLED: false };", "window.DADMIN_API_NAMESPACE = 'dadmin/v2';"])
        env.update(CERTIFICATE_FILE='/run/platform/tls.crt', CERTIFICATE_KEY_FILE='/run/platform/tls.key')
        upstream = f'pd-pingdirectory.identity-{c["directory_colour"]}.svc.cluster.local'
        nginx = f'''server {{
    listen 6443 ssl;
    ssl_certificate /run/platform/tls.crt;
    ssl_certificate_key /run/platform/tls.key;
    location / {{ root /opt/out/instance/html; index index.html; }}
    location /dadmin/ {{
        proxy_pass https://{upstream}:443;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /run/platform/ca.crt;
        proxy_ssl_server_name on;
        proxy_ssl_name {upstream};
        proxy_set_header Authorization $http_authorization;
    }}
}}
'''
        data = {'instance/html/delegator/config.js': config_js, 'instance/etc/conf.d/default.conf': nginx}
    files = {f'profile-{i}': value for i,value in enumerate(data.values())}
    items = [{'key': f'profile-{i}', 'path': path} for i,path in enumerate(data)]
    volumes = [{'name': 'profile', 'configMap': {'name': name+'-profile', 'defaultMode': 0o755, 'items': items}},
        {'name': 'runtime', 'secret': {'secretName': name+'-runtime'}}]
    container = {'name': name, 'image': image, 'env': [{'name': k, 'value': v} for k,v in env.items()],
        'ports': [{'containerPort': port}], 'resources': {'requests': {'cpu': '100m', 'memory': '512Mi' if central else '64Mi'},
            'limits': {'memory': '2Gi' if central else '256Mi'}},
        'volumeMounts': [{'name': 'profile', 'mountPath': '/opt/in', 'readOnly': True},
            {'name': 'runtime', 'mountPath': '/run/platform', 'readOnly': True}],
        'startupProbe': {'tcpSocket': {'port': port}, 'periodSeconds': 10, 'failureThreshold': 60},
        'readinessProbe': {'httpGet': {'path': '/actuator/health' if central else '/delegator/', 'port': port, 'scheme': 'HTTPS'}}}
    if central:
        container['volumeMounts'].append({'name': 'data', 'mountPath': '/opt/out'})
        container['envFrom'] = [{'secretRef': {'name': 'pingcentral-devops', 'optional': True}}]
    doc = workload(name, ns, container, volumes, storage='10Gi' if central else None,
        enabled=c['central_enabled' if central else 'delegator_enabled'])
    # Ping startup copies /opt/in into /opt/out. Updating the projected ConfigMap
    # alone cannot refresh the running process's configuration.
    doc['spec']['template']['metadata']['annotations'] = {
        'checksum/profile': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    return [obj('ConfigMap', name+'-profile', ns, data=files), doc, service(name, ns, port)]


def export():
    c = config()
    import gitops
    g = gitops.config()
    for name,ns in APPLICATIONS.items():
        docs = metrics(c) if name == 'metrics' else ping_workload(c, name)
        folder = ROOT/'gitops/workloads'/name
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'resources.yaml').write_text(yaml.safe_dump_all([gitops.protect(d) for d in docs], sort_keys=False), newline='\n')
        spec = gitops.app_project(g)['spec']
        spec['description'] = 'Optional platform services; secrets and routing stay operator-owned'
        spec['destinations'] = [{'server': 'https://kubernetes.default.svc', 'namespace': x} for x in ([ns]+PING_NAMESPACES if name == 'metrics' else [ns])]
        spec['namespaceResourceWhitelist'].append({'group': '', 'kind': 'ServiceAccount'})
        project_doc = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AppProject',
            'metadata': {'name': name, 'namespace': 'argocd'}, 'spec': spec}
        app = gitops.application(g, name, ns)
        app['spec']['project'] = name
        # Optional services start disabled. Explicit enable is a reviewed Git change.
        app['spec'].pop('ignoreDifferences', None)
        for filename, value in [(name+'-project.yaml', project_doc), (name+'.yaml', app)]:
            (ROOT/'gitops/applications'/filename).write_text(yaml.safe_dump(value, sort_keys=False), newline='\n')
    integration_plan(c)


def integration_plan(c):
    """Reviewable files only: existing PF/Directory configuration is never overwritten."""
    project.BUILD.mkdir(exist_ok=True)
    origin = 'https://'+c['delegator_hostname']
    client = {'clientId': 'dadmin', 'name': 'Directory Delegated Admin',
        'clientAuth': {'type': 'NONE'}, 'grantTypes': ['AUTHORIZATION_CODE', 'REFRESH_TOKEN'],
        'redirectUris': [origin+'/delegator/', origin+'/delegator/silent_renew.html'],
        'requireProofKeyForCodeExchange': True, 'restrictScopes': True,
        'restrictedScopes': ['openid'],
        'exclusiveScopes': ['urn:pingidentity:directory-delegated-admin'],
        'defaultAccessTokenManagerRef': {'id': 'jwt'},
        'oidcPolicy': {'policyGroup': {'id': 'pingdelegator'}}}
    (project.BUILD/'delegator-pf-client.json').write_text(json.dumps(client, indent=2)+'\n')
    common = (ROOT/'profiles/pingdirectory/pd.profile/dsconfig/50-delegated-admin.dsconfig').read_text().replace('${USER_BASE_DN}', project.config()['base_dn'])
    signing = (SOURCE/'delegator.dsconfig').read_text()
    (project.BUILD/'delegator-common.dsconfig').write_text(common)
    (project.BUILD/'delegator-signing.dsconfig').write_text(signing)
    (project.BUILD/'delegator.dsconfig').write_text(common+'\n'+signing)
    (project.BUILD/'central-environments.json').write_text(json.dumps({'environments': [
        {'name': 'lab-'+colour, 'pingfederate_admin': f'https://pf-{colour}-pingfederate-admin.identity-federate-{colour}.svc.cluster.local:9999',
            'pingaccess_admin': f'https://pa-{colour}-pingaccess-admin.identity-access-{colour}.svc.cluster.local:9000',
            'note': 'Inventory for the setup wizard, not a PingCentral API payload. Configure verified TLS trust and API authentication first.'}
        for colour in ('blue', 'green')]}, indent=2)+'\n')


def prepare():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    ca = ROOT/'.secrets'
    if not (ca/'ca.key').exists(): raise ValueError('Create or restore the existing internal CA first')
    # Preserve the existing CA key/identity; the older dev certificate lacks keyUsage.
    # Use a corrected trust certificate for strict TLS clients without rotating the CA.
    if not (PRIVATE/'ca.crt').exists():
        subject = project.run(['openssl', 'x509', '-in', ca/'ca.crt', '-noout', '-subject', '-nameopt', 'compat'], capture=True).strip().split('=', 1)[1].strip()
        serial = project.run(['openssl', 'x509', '-in', ca/'ca.crt', '-noout', '-serial'], capture=True).strip().split('=', 1)[1]
        ext = PRIVATE/'ca.cnf'
        ext.write_text('basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n')
        project.run(['openssl', 'req', '-new', '-key', ca/'ca.key', '-subj', subject, '-out', PRIVATE/'ca.csr'], capture=True)
        project.run(['openssl', 'x509', '-req', '-in', PRIVATE/'ca.csr', '-signkey', ca/'ca.key', '-set_serial', '0x'+serial,
            '-extfile', ext, '-days', '3650', '-out', PRIVATE/'ca.crt'], capture=True)
    c = config()
    for name in ('grafana-password', 'db-password', 'keystore.pin'):
        path = PRIVATE/name
        if not path.exists(): path.write_text(secrets.token_urlsafe(32))
    for name,ns,host in [('metrics', 'monitoring', c['monitoring_hostname']),
            ('pingcentral', 'identity-central', c['central_hostname']), ('delegator', 'identity-delegator', c['delegator_hostname'])]:
        cert = PRIVATE/(name+'.crt')
        if cert.exists(): continue
        ext = PRIVATE/(name+'.cnf')
        ext.write_text('subjectAltName='+','.join('DNS:'+x for x in [name, host, name+'.'+ns+'.svc.cluster.local', 'localhost', 'ping-metrics'])+
            '\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nextendedKeyUsage=serverAuth\n')
        for args in [['openssl', 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN='+name,
                '-keyout', PRIVATE/(name+'.key'), '-out', PRIVATE/(name+'.csr')],
            ['openssl', 'x509', '-req', '-in', PRIVATE/(name+'.csr'), '-CA', ca/'ca.crt', '-CAkey', ca/'ca.key',
                '-CAcreateserial', '-days', '365', '-extfile', ext, '-out', cert]]:
            project.run(args, capture=True)
    if not (PRIVATE/'keystore.p12').exists():
        project.run(['openssl', 'pkcs12', '-export', '-name', 'pingcentral', '-inkey', PRIVATE/'pingcentral.key',
            '-in', PRIVATE/'pingcentral.crt', '-certfile', ca/'ca.crt', '-out', PRIVATE/'keystore.p12',
            '-passout', 'file:'+str(PRIVATE/'keystore.pin')], capture=True)
    print('Prepared private platform credentials and CA-signed TLS. No cloud requests sent.')


def install_secrets():
    license_path = ROOT/'.secrets/PingCentral.lic'
    creds = {n: os.environ.get(n, '') for n in ('PING_IDENTITY_DEVOPS_USER', 'PING_IDENTITY_DEVOPS_KEY')}
    if config()['central_enabled'] and not license_path.exists() and not all(creds.values()):
        raise ValueError('Provide .secrets/PingCentral.lic or both Ping DevOps environment variables')
    project.check_context(project.config())
    prepare()
    ca = (PRIVATE/'ca.crt').read_bytes()
    def put(ns, name, data):
        project.apply(obj('Secret', name, ns, data={k: base64.b64encode(v).decode() for k,v in data.items()}))
    for ns in set(APPLICATIONS.values()) | set(PING_NAMESPACES):
        project.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns}})
    tls = {'tls.crt': (PRIVATE/'metrics.crt').read_bytes()+ca, 'tls.key': (PRIVATE/'metrics.key').read_bytes(), 'ca.crt': ca}
    for ns in ['monitoring']+PING_NAMESPACES: put(ns, 'metrics-tls', tls)
    put('monitoring', 'grafana-auth', {'admin-password': (PRIVATE/'grafana-password').read_bytes()})
    for name in ('delegator', 'pingcentral'):
        data = {'tls.crt': (PRIVATE/(name+'.crt')).read_bytes()+ca, 'tls.key': (PRIVATE/(name+'.key')).read_bytes(), 'ca.crt': ca}
        if name == 'pingcentral':
            data.update({n: (PRIVATE/n).read_bytes() for n in ('db-password', 'keystore.p12', 'keystore.pin')})
            if license_path.exists(): data['pingcentral.lic'] = license_path.read_bytes()
            elif all(creds.values()):
                put(APPLICATIONS[name], 'pingcentral-devops', {n: v.encode() for n,v in creds.items()})
        put(APPLICATIONS[name], name+'-runtime', data)
    print('Installed runtime secrets. No workloads synced.')


def ingresses(c, g):
    if not g['origin_certificate_arn'] or not g['cloudflare_ipv4_cidrs']:
        raise ValueError('Configure ACM certificate and Cloudflare ranges in gitops/config.json')
    for cidr in g['cloudflare_ipv4_cidrs']:
        n = ipaddress.ip_network(cidr)
        if n.version != 4 or n.prefixlen == 0: raise ValueError('Explicit Cloudflare IPv4 ranges required')
    docs = []
    for name,host,port,path,ready in [('grafana', c['monitoring_hostname'], 3000, '/api/health', c['metrics_enabled']),
            ('delegator', c['delegator_hostname'], 6443, '/delegator/', c['delegator_enabled'] and c['delegator_integration_verified']),
            ('pingcentral', c['central_hostname'], 9022, '/actuator/health', c['central_enabled'] and c['central_password_changed'] and c['central_tls_verified'])]:
        if not ready: continue
        ns = APPLICATIONS['metrics' if name == 'grafana' else name]
        doc = obj('Ingress', name, ns, spec={'ingressClassName': 'alb', 'rules': [{'host': host,
            'http': {'paths': [{'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': name, 'port': {'number': port}}}}]}}]})
        doc['apiVersion'] = 'networking.k8s.io/v1'
        doc['metadata']['annotations'] = {'alb.ingress.kubernetes.io/scheme': 'internet-facing',
            'alb.ingress.kubernetes.io/target-type': 'ip', 'alb.ingress.kubernetes.io/listen-ports': '[{"HTTPS":443}]',
            'alb.ingress.kubernetes.io/backend-protocol': 'HTTPS', 'alb.ingress.kubernetes.io/healthcheck-protocol': 'HTTPS',
            'alb.ingress.kubernetes.io/healthcheck-path': path, 'alb.ingress.kubernetes.io/success-codes': '200-399',
            'alb.ingress.kubernetes.io/certificate-arn': g['origin_certificate_arn'],
            'alb.ingress.kubernetes.io/inbound-cidrs': ','.join(g['cloudflare_ipv4_cidrs'])}
        docs.append(doc)
    return docs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['render', 'prepare', 'secrets', 'ingress', 'enable'])
    p.add_argument('--service', choices=list(APPLICATIONS))
    a = p.parse_args()
    if a.command == 'render': export()
    elif a.command == 'prepare': prepare()
    elif a.command == 'secrets': install_secrets()
    elif a.command == 'enable':
        if not a.service: raise ValueError('--service is required')
        c = config(); c[{'metrics': 'metrics_enabled', 'delegator': 'delegator_enabled', 'pingcentral': 'central_enabled'}[a.service]] = True
        (ROOT/'platform-services.json').write_text(json.dumps(c, indent=2)+'\n')
        print('Enabled local rendering only. Run gitops-export and review before syncing.')
    else:
        import gitops
        project.check_context(project.config())
        for doc in ingresses(config(), gitops.config()): project.apply(doc)


if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        raise SystemExit('Error: '+str(e))
