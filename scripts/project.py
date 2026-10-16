#!/usr/bin/env python3
"""Remote development workflow; no cloud mutations during validate/render."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build'
NAMESPACES = ('identity-blue', 'identity-green', 'identity-proxy')
COLOURS = ('blue', 'green')
CHART_SHA = '9fac7be41a22f25fc34ad81577ed49fec762c805011510d9d634c44cdd1a0872'

def run(args, *, capture=False, data=None, env=None):
    if shutil.which(str(args[0])) is None:
        raise FileNotFoundError(f"Required executable '{args[0]}' was not found. Install it and add it to PATH.")
    return subprocess.run([str(a) for a in args], check=True, text=True, input=data,
                          capture_output=capture, cwd=ROOT, env=env).stdout if capture else subprocess.run(
                          [str(a) for a in args], check=True, text=True, input=data, cwd=ROOT, env=env)

def profile_shell():
    shell = shutil.which('sh')
    if shell:
        return shell
    if os.name == 'nt':
        candidates = [Path(os.environ.get('ProgramFiles', 'C:/Program Files'))/'Git/bin/sh.exe',
                      Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Programs/Git/bin/sh.exe']
        git = shutil.which('git')
        if git:
            candidates.insert(0, Path(git).resolve().parent.parent/'bin/sh.exe')
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError("Profile validation requires 'sh'. Install Git for Windows (which includes sh), add sh to PATH, or use the Linux dev container.")

def config():
    c = json.loads((ROOT / 'config.json').read_text())
    if c['replicas'] != 3:
        raise ValueError('This routing/profile implementation supports exactly three replicas per colour.')
    if c['initial_seed_colour'] not in COLOURS:
        raise ValueError('Invalid seed colour')
    if c['directory_capacity_type'] not in ('SPOT', 'ON_DEMAND'):
        raise ValueError('capacity type must be SPOT or ON_DEMAND')
    if c['chart_version'] != '0.16.0':
        raise ValueError('Vendor and revalidate a new chart before changing chart_version')
    if not re.fullmatch(r'(dc|o)=[A-Za-z0-9-]+(,dc=[A-Za-z0-9-]+)*', c['base_dn']):
        raise ValueError('Use a simple dc= or o= base DN; extend validation for other directory naming schemes.')
    return c

def tf_outputs():
    return {k: v['value'] for k, v in json.loads(run(['terraform', '-chdir=terraform', 'output', '-json'], capture=True)).items()}

def apply(obj):
    run(['kubectl', 'apply', '-f', '-'], data=yaml.safe_dump(obj))

def check_context(c):
    out = tf_outputs()
    if out['cluster_name'] != c['cluster_name'] or out['region'] != c['region']:
        raise ValueError('Terraform outputs and config.json differ.')
    context = run(['kubectl','config','current-context'], capture=True).strip()
    if context != out['cluster_arn']:
        raise ValueError('Wrong Kubernetes context/account; run make kubeconfig first.')
    return out

def routing_colour():
    r = subprocess.run(['kubectl', '-n', 'identity-proxy', 'get', 'configmap', 'pd-routing', '-o', 'json', '--ignore-not-found'],
                       check=True, capture_output=True, text=True)
    if not r.stdout.strip():
        return None
    value = json.loads(r.stdout)['data']['active_colour']
    if value not in COLOURS:
        raise ValueError('Invalid persisted routing colour')
    return value

def common_values(c):
    import platform_services
    import federate
    import access
    platform = platform_services.config()
    base_attribute, base_value = c['base_dn'].split(',', 1)[0].split('=', 1)
    envs = {
        'BASE_ATTRIBUTE': base_attribute, 'BASE_VALUE': base_value,
        'BASE_CLASS': 'domain' if base_attribute == 'dc' else 'organization',
        'PING_IDENTITY_ACCEPT_EULA': 'YES' if c['accept_eula'] else 'NO',
        'USER_BASE_DN': c['base_dn'], 'MAX_HEAP_SIZE': '4g',
        'PD_DELEGATOR_PUBLIC_HOSTNAME': platform['delegator_hostname'],
        'PD_DELEGATOR_PUBLIC_PORT_HTTPS': '443',
        'PF_ENGINE_PUBLIC_HOSTNAME': federate.config()['engine_hostname'],
        'PF_ENGINE_PUBLIC_PORT_HTTPS': '443',
        'PA_ENGINE_PUBLIC_HOSTNAME': access.config()['engine_hostname'],
        'ROOT_USER_DN': 'cn=administrator', 'MAKELDIF_USERS': '0',
        'RETRY_TIMEOUT_SECONDS': '900', 'SERVER_PROFILE_URL': '',
        'SECRETS_DIR': '/run/ping-secrets', 'PD_LICENSE_DIR': '/run/ping-secrets', 'LICENSE_DIR': '/run/ping-secrets',
        'KEYSTORE_FILE': '/run/ping-secrets/keystore.p12', 'KEYSTORE_PIN_FILE': '/run/ping-secrets/keystore.pin',
        'TRUSTSTORE_FILE': '/run/ping-secrets/truststore.p12', 'TRUSTSTORE_PIN_FILE': '/run/ping-secrets/truststore.pin',
    }
    zone_reader = {
        'name': 'read-availability-zone', 'image': 'pingidentity/pingtoolkit:2609',
        'command': ['sh', '-ec'],
        'args': ['zone=$(kubectl get node "$NODE_NAME" -o jsonpath=\'{.metadata.labels.topology\\.kubernetes\\.io/zone}\'); test -n "$zone"; printf "%s" "$zone" > /runtime/availability-zone'],
        'env': [{'name': 'NODE_NAME', 'valueFrom': {'fieldRef': {'fieldPath': 'spec.nodeName'}}}],
        'volumeMounts': [{'name': 'runtime-zone', 'mountPath': '/runtime'}],
        'securityContext': {'runAsUser': 9031, 'runAsGroup': 0, 'allowPrivilegeEscalation': False},
    }
    return {
        'global': {'image': {'tag': c['base_image_tag']}, 'envs': envs,
                   'rbac': {'serviceAccountName': 'ping-runtime', 'applyServiceAccountToWorkload': True},
                   'includeInitContainers': ['prepare-runtime', 'read-availability-zone'],
                   'includeVolumes': ['runtime-zone', 'runtime-secrets', 'runtime-secret-files'],
                   'volumeMounts': [{'name': 'runtime-zone', 'mountPath': '/runtime'},
                                    {'name': 'runtime-secret-files', 'mountPath': '/run/ping-secrets', 'readOnly': True}],
                   'workload': {'podDisruptionBudget': {'enabled': True, 'minAvailable': 2},
                       'topologySpreadConstraints': [
                           {'maxSkew': 1, 'topologyKey': 'topology.kubernetes.io/zone', 'whenUnsatisfiable': 'DoNotSchedule'},
                           {'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname', 'whenUnsatisfiable': 'DoNotSchedule'}]}},
        'initContainers': {'read-availability-zone': zone_reader, 'prepare-runtime': {
            'name': 'prepare-runtime', 'image': 'pingidentity/pingtoolkit:2609',
            'command': ['sh', '-ec'], 'args': [(ROOT/'scripts/prepare_runtime.sh').read_text()],
            'securityContext': {'runAsUser': 9031, 'runAsGroup': 0, 'allowPrivilegeEscalation': False},
            'volumeMounts': [{'name': 'runtime-secrets', 'mountPath': '/run/projected-secrets', 'readOnly': True},
                             {'name': 'runtime-secret-files', 'mountPath': '/run/ping-secrets'},
                             {'name': 'out-dir', 'mountPath': '/opt/out'}]}},
        'volumes': {'runtime-zone': {'emptyDir': {}},
                    'runtime-secrets': {'secret': {'secretName': 'pd-runtime'}},
                    'runtime-secret-files': {'emptyDir': {'medium': 'Memory'}}},
    }

def values(c, target, repositories=None):
    if target not in (*COLOURS, 'proxy'):
        raise ValueError('Invalid deployment target')
    v = common_values(c)
    kind = 'pingdirectoryproxy' if target == 'proxy' else 'pingdirectory'
    v[kind] = {'enabled': True, 'container': {'replicaCount': 3},
               'image': {'tag': c['image_tag']}, 'envs': {}}
    p = v[kind]
    if repositories:
        p['image']['repositoryFqn'] = repositories[kind]
    else:
        p['image']['repositoryFqn'] = f'123456789012.dkr.ecr.{c["region"]}.amazonaws.com/{c["cluster_name"]}/{kind}'
    p['container']['terminationGracePeriodSeconds'] = 300
    p['container']['nodeSelector'] = {'eks.amazonaws.com/capacityType': c['directory_capacity_type'] if target != 'proxy' else 'SPOT'}
    p['container']['resources'] = {'requests': {'cpu': '1500m' if target != 'proxy' else '250m',
                                               'memory': '6Gi' if target != 'proxy' else '1Gi'},
                                    'limits': {'memory': '8Gi' if target != 'proxy' else '2Gi'}}
    if target in COLOURS:
        # Each release has real per-pod DNS; green references the existing blue seed.
        p['envs'].update({'K8S_CLUSTERS': 'blue green', 'K8S_CLUSTER': target,
                         'K8S_SEED_CLUSTER': c['initial_seed_colour'], 'K8S_NUM_REPLICAS': '3',
                         'K8S_POD_HOSTNAME_PREFIX': 'pd-pingdirectory-',
                         'K8S_POD_HOSTNAME_SUFFIX': f'.pd-pingdirectory-cluster.identity-{target}.svc.cluster.local',
                         'K8S_SEED_HOSTNAME_PREFIX': 'pd-pingdirectory-',
                         'K8S_SEED_HOSTNAME_SUFFIX': f'.pd-pingdirectory-cluster.identity-{c["initial_seed_colour"]}.svc.cluster.local'})
        p['workload'] = {'type': 'StatefulSet', 'statefulSet': {'persistentvolume': {'enabled': True,
                       'volumes': {'out-dir': {'mountPath': '/opt/out', 'persistentVolumeClaim': {
                           'accessModes': ['ReadWriteOnce'], 'storageClassName': c['storage_class'],
                           'resources': {'requests': {'storage': c['directory_storage']}}}}}}}}
    else:
        p['envs']['MAX_HEAP_SIZE'] = '1g'
        # Proxy instances are ephemeral and have no shared out-dir volume.
        init = v['initContainers']['prepare-runtime']
        init['volumeMounts'] = [mount for mount in init['volumeMounts'] if mount['name'] != 'out-dir']
        p['container']['env'] = [{'name': 'ACTIVE_COLOUR', 'valueFrom': {'configMapKeyRef': {'name': 'pd-routing', 'key': 'active_colour'}}}]
        p['workload'] = {'type': 'StatefulSet', 'statefulSet': {'persistentvolume': {'enabled': False}}}
    return v

def chart():
    p = ROOT / 'vendor-ping-devops-0.16.0.tgz'
    if hashlib.sha256(p.read_bytes()).hexdigest() != CHART_SHA:
        raise ValueError('Vendored chart checksum mismatch')
    return p

def render(c, target, repos=None):
    BUILD.mkdir(exist_ok=True)
    f = BUILD / f'{target}.values.yaml'
    f.write_text(yaml.safe_dump(values(c, target, repos), sort_keys=False))
    ns = f'identity-{target}'
    result = run(['helm', 'template', 'pd', chart(), '-n', ns, '--kube-version', '1.35.0', '-f', f], capture=True)
    manifests = list(yaml.safe_load_all(result))
    import observability
    manifests = observability.attach(manifests, namespace=ns)
    import platform_services
    manifests = platform_services.attach(manifests, namespace=ns)
    # Full parser also rejects chart/values indentation bugs before any mutation.
    for obj in manifests:
        if obj and obj.get('kind') == 'StatefulSet':
            pod = obj['spec']['template']['spec']
            names = [vol['name'] for vol in pod.get('volumes', [])]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate generated volume names')
            available = set(names) | {claim['metadata']['name'] for claim in obj['spec'].get('volumeClaimTemplates', [])}
            for container in pod.get('initContainers', []) + pod['containers']:
                for mount in container.get('volumeMounts', []):
                    if mount['name'] not in available:
                        raise ValueError(f'{target}: container {container["name"]} mounts undefined volume {mount["name"]}')
            for con in pod['topologySpreadConstraints']:
                if 'labelSelector' not in con:
                    raise ValueError('Chart failed to add spread selector')
    (BUILD / f'{target}.manifests.yaml').write_text(yaml.safe_dump_all(manifests, sort_keys=False))
    return f

def validate_evidence(e, c, target, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if target not in COLOURS:
        raise ValueError('Only blue or green can be promoted')
    if e.get('cluster_name') != c['cluster_name'] or e.get('target_colour') != target:
        raise ValueError('Evidence does not match cluster and target')
    if e.get('image_tag') != c['image_tag'] or e.get('base_dn') != c['base_dn']:
        raise ValueError('Evidence does not match image/base DN')
    ts = dt.datetime.fromisoformat(e['verified_at'].replace('Z', '+00:00'))
    if ts.tzinfo is None or not 0 <= (now - ts).total_seconds() <= 300:
        raise ValueError('Verification evidence must be from the last five minutes')
    flags = ('replication_verified_all_base_dns', 'writes_reconciled', 'rollback_compatible', 'direct_writers_accounted_for')
    if any(e.get(name) is not True for name in flags):
        raise ValueError('All verification attestations must be true')
    if not e.get('operator') or not e.get('evidence_notes'):
        raise ValueError('Record the operator and verification evidence notes')

def ready_pods(namespace, product, image_tag=None):
    data = json.loads(run(['kubectl', '-n', namespace, 'get', 'pods', '-l',
                          f'app.kubernetes.io/name={product}', '-o', 'json'], capture=True))
    pods = data['items']
    if len(pods) != 3:
        raise ValueError(f'{namespace}: expected exactly three pods')
    names = []
    zones = set()
    for pod in pods:
        if pod['metadata'].get('deletionTimestamp') or not any(x['type'] == 'Ready' and x['status'] == 'True'
                         for x in pod.get('status', {}).get('conditions', [])):
            raise ValueError(f'{namespace}: pod not ready')
        if image_tag and not any(con['name'] == product and con['image'].endswith(':' + image_tag)
                                 for con in pod['spec']['containers']):
            raise ValueError(f'{namespace}: a candidate pod does not run the verified image tag')
        node = json.loads(run(['kubectl', 'get', 'node', pod['spec']['nodeName'], '-o', 'json'], capture=True))
        zones.add(node['metadata']['labels']['topology.kubernetes.io/zone'])
        names.append(pod['metadata']['name'])
    if len(zones) != 3:
        raise ValueError(f'{namespace}: replicas must occupy three distinct AZs')
    return sorted(names)

def bootstrap(c):
    for ns in NAMESPACES:
        apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns}})
        apply({'apiVersion':'v1','kind':'ServiceAccount','metadata':{'name':'ping-runtime','namespace':ns}})
    apply({'apiVersion':'rbac.authorization.k8s.io/v1','kind':'ClusterRole',
           'metadata':{'name':'ping-read-node-zone'}, 'rules':[{'apiGroups':[''],'resources':['nodes'],'verbs':['get']}]})
    apply({'apiVersion':'rbac.authorization.k8s.io/v1','kind':'ClusterRoleBinding',
           'metadata':{'name':'ping-read-node-zone'},
           'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':'ping-read-node-zone'},
           'subjects':[{'kind':'ServiceAccount','name':'ping-runtime','namespace':ns} for ns in NAMESPACES]})
    apply({'apiVersion':'storage.k8s.io/v1','kind':'StorageClass','metadata':{'name':c['storage_class']},
           'provisioner':'ebs.csi.aws.com','reclaimPolicy':'Retain','volumeBindingMode':'WaitForFirstConsumer',
           'allowVolumeExpansion':True,'parameters':{'type':'gp3','encrypted':'true'}})

def secrets(c):
    check_context(c)
    directory = ROOT / '.secrets'
    directory.mkdir(mode=0o700, exist_ok=True)
    for name in ('root-user-password','admin-user-password','encryption-password','keystore.pin','truststore.pin',
                 'demo-user-password', 'federate-ldap-password'):
        p = directory / name
        if not p.exists():
            p.write_bytes(base64.urlsafe_b64encode(os.urandom(32)) + b'\n')
            p.chmod(0o600)
    if not (directory / 'ca.crt').exists():
        run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','365',
             '-subj','/CN=DarkEdges internal development CA','-keyout',directory/'ca.key','-out',directory/'ca.crt'])
    if not (directory / 'keystore.p12').exists():
        san = ['DNS:localhost', 'DNS:pd-pingdirectoryproxy.identity-proxy.svc.cluster.local']
        san += [f'DNS:pd-pingdirectory.identity-{col}.svc.cluster.local' for col in COLOURS]
        san += [f'DNS:*.pd-pingdirectory-cluster.identity-{col}.svc.cluster.local' for col in COLOURS]
        san += ['DNS:*.pd-pingdirectoryproxy-cluster.identity-proxy.svc.cluster.local']
        (directory/'extensions.cnf').write_text('subjectAltName='+','.join(san)+'\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=CA:FALSE\n')
        run(['openssl','req','-new','-newkey','rsa:3072','-nodes','-subj','/CN=localhost',
             '-keyout',directory/'server.key','-out',directory/'server.csr'])
        run(['openssl','x509','-req','-in',directory/'server.csr','-CA',directory/'ca.crt','-CAkey',directory/'ca.key',
             '-CAcreateserial','-days','365','-extfile',directory/'extensions.cnf','-out',directory/'server.crt'])
        run(['openssl','pkcs12','-export','-name','ping','-inkey',directory/'server.key','-in',directory/'server.crt',
             '-certfile',directory/'ca.crt','-out',directory/'keystore.p12','-passout','env:PING_KEYSTORE_EXPORT_PIN'],
            env={**os.environ, 'PING_KEYSTORE_EXPORT_PIN': (directory/'keystore.pin').read_text().rstrip('\r\n')})
        run(['keytool','-importcert','-noprompt','-alias','project-ca','-file',directory/'ca.crt','-keystore',directory/'truststore.p12',
             '-storetype','PKCS12','-storepass:file',directory/'truststore.pin'])
    for store in ('keystore', 'truststore'):
        run(['keytool', '-list', '-keystore', directory/f'{store}.p12', '-storetype', 'PKCS12',
             '-storepass:file', directory/f'{store}.pin'], capture=True)
    required = ['root-user-password','admin-user-password','encryption-password','keystore.pin','truststore.pin','keystore.p12','truststore.p12',
                'ca.crt','demo-user-password','federate-ldap-password']
    for p in directory.iterdir():
        p.chmod(0o600)
    data = {name:base64.b64encode((directory/name).read_bytes()).decode() for name in required}
    import oauth_client_secrets
    for name in oauth_client_secrets.runtime_files():
        path = oauth_client_secrets.PRIVATE/name
        if not path.is_file():
            raise ValueError('Run make oauth-client-secrets before installing Directory runtime secrets')
        data[name] = base64.b64encode(path.read_bytes()).decode()
    for name in ('PingDirectory.lic','PingDirectoryProxy.lic'):
        if (directory/name).exists(): data[name] = base64.b64encode((directory/name).read_bytes()).decode()
    if not all(name in data for name in ('PingDirectory.lic','PingDirectoryProxy.lic')):
        raise ValueError('Provide both authorized PingDirectory.lic and PingDirectoryProxy.lic files in .secrets.')
    bootstrap(c)
    for ns in NAMESPACES:
        apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':'pd-runtime','namespace':ns},'type':'Opaque','data':data})
    if routing_colour() is None:
        apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'pd-routing','namespace':'identity-proxy'},
               'data':{'active_colour':'blue'}})
    print('Development secrets installed. No private keys or credentials are saved in the project archive.')

def dsconfig(pod, extra):
    run(['kubectl','-n','identity-proxy','exec',pod,'-c','pingdirectoryproxy','--','dsconfig','--noPropertiesFile','--hostname','localhost','--port','1636',
         '--useSSL','--trustStorePath','/run/ping-secrets/truststore.p12','--trustStorePasswordFile','/run/ping-secrets/truststore.pin',
         '--bindDN','cn=administrator','--bindPasswordFile','/run/ping-secrets/root-user-password','--no-prompt',*extra])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['validate','render','kubeconfig','secrets','build-images','deploy','promote','status'])
    parser.add_argument('--target', choices=['blue','green','proxy'], default='blue')
    parser.add_argument('--evidence', default='')
    a = parser.parse_args()
    c = config()
    BUILD.mkdir(exist_ok=True)
    if a.command == 'validate':
        shell = profile_shell()
        for product in ('pingdirectory','pingdirectoryproxy','pingfederate','pingaccess'):
            for p in (ROOT/'profiles'/product/'hooks').glob('*.sh*'):
                run([shell,'-n',p.as_posix()])
        for target in (*COLOURS,'proxy'):
            render(c, target)
        print('Validated profile hook syntax and rendered all three Helm releases.')
    elif a.command == 'render':
        print(render(c, a.target))
    elif a.command == 'kubeconfig':
        out = tf_outputs()
        if out['cluster_name'] != c['cluster_name'] or out['region'] != c['region']:
            raise ValueError('Terraform outputs and config.json do not identify the same cluster.')
        run(['aws','eks','update-kubeconfig','--name',out['cluster_name'],'--region',out['region']])
    elif a.command == 'secrets':
        secrets(c)
    elif a.command == 'build-images':
        out = tf_outputs()
        registry = next(iter(out['repositories'].values())).split('/')[0]
        password = run(['aws','ecr','get-login-password','--region',out['region']], capture=True)
        run(['docker','login','--username','AWS','--password-stdin',registry], data=password)
        for product, repo in out['repositories'].items():
            if product in ('pingfederate','pingaccess'):
                continue  # PF has independently versioned blue/green image tags.
            run(['docker','build','--platform','linux/amd64','--build-arg',f'BASE_TAG={c["base_image_tag"]}',
                 '-f',f'images/{product}.Dockerfile','-t',f'{repo}:{c["image_tag"]}','.'])
            run(['docker','push',f'{repo}:{c["image_tag"]}'])
    elif a.command == 'deploy':
        if c.get('deployment_manager') == 'argocd':
            raise ValueError('Argo CD owns workload deployment. Run make gitops-export, commit, then sync the candidate application.')
        if not c['accept_eula']:
            raise ValueError('Review the Ping EULA and set accept_eula=true to run these images.')
        out = check_context(c)
        if a.target != c['initial_seed_colour']:
            ready_pods(f'identity-{c["initial_seed_colour"]}', 'pingdirectory')
        f = render(c, a.target, out['repositories'])
        run(['helm','upgrade','--install','pd',chart(),'-n',f'identity-{a.target}','-f',f,'--wait','--timeout','30m'])
    elif a.command == 'promote':
        if not a.evidence:
            raise ValueError('Supply a recent operator verification file; see docs/promotion-evidence.example.json.')
        e = json.loads(Path(a.evidence).read_text())
        validate_evidence(e,c,a.target)
        check_context(c)
        ready_pods(f'identity-{a.target}', 'pingdirectory', c['image_tag'])
        pods = ready_pods('identity-proxy','pingdirectoryproxy')
        # Persist first: a proxy restarted during the update must use the target colour.
        run(['kubectl','-n','identity-proxy','patch','configmap','pd-routing','--type','merge','-p',
             json.dumps({'data':{'active_colour':a.target}})])
        for pod in pods:
            extra=['set-load-balancing-algorithm-prop','--algorithm-name','active-directory']
            for index in range(3): extra += ['--set',f'backend-server:{a.target}-{index}']
            dsconfig(pod,extra)
        print(f'Proxy backend configuration points to {a.target}. Keep both colours replicating; verify connection drain and final writes.')
    else:
        for ns in NAMESPACES:
            run(['kubectl','-n',ns,'get','pods','-o','wide'])
        print('Active routing colour:', routing_colour())
        for colour in COLOURS:
            run(['kubectl','-n',f'identity-{colour}','exec','pd-pingdirectory-0','-c','pingdirectory','--','dsreplication','status','--noPropertiesFile','--showAll',
                 '--hostname','localhost','--port','1636','--useSSL','--trustStorePath','/run/ping-secrets/truststore.p12',
                 '--trustStorePasswordFile','/run/ping-secrets/truststore.pin','--trustStoreFormat','PKCS12','--adminUID','admin',
                 '--adminPasswordFile','/run/ping-secrets/admin-user-password','--no-prompt'])

if __name__ == '__main__':
    try: main()
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
