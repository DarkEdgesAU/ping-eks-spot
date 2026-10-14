#!/usr/bin/env python3
"""Independent PF colours, local rendering, explicit deployment and gated routing."""
import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import uuid
import yaml
import project

ROOT = project.ROOT
LEGACY_NS = 'identity-federate'


def namespace(colour):
    if colour not in project.COLOURS: raise ValueError('Invalid PF colour')
    return f'identity-federate-{colour}'


def tls_name(colour):
    return SECRET/f'federate-{colour}-{namespace(colour)}'
SECRET = ROOT/'.secrets'
LDAP_ID = 'LDAP-D803C87FAB2ADFB4B0A947B64BA6F0C6093A5CA3'
EVIDENCE_FLAGS = ('terraform_applied','configuration_replicated','ldap_verified','login_verified',
                 'session_and_token_compatibility_verified','rollback_verified')


def config():
    path = ROOT/'federate.json'
    c = json.loads((path if path.exists() else ROOT/'federate.example.json').read_text())
    if c['engine_replicas'] != 3 or c['initial_colour'] not in project.COLOURS:
        raise ValueError('Federate requires three engines and a blue/green initial colour')
    for key in ('admin_hostname', 'engine_hostname'):
        if not project.re.fullmatch(r'[a-z0-9.-]+', c[key]):
            raise ValueError('Invalid hostname')
    for cidr in c['cloudflare_ipv4_cidrs']:
        net = ipaddress.ip_network(cidr)
        if net.version != 4 or net.prefixlen == 0:
            raise ValueError('Origin ingress requires explicit IPv4 ranges')
    return c


def random_file(path, size=32):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.urlsafe_b64encode(os.urandom(size)).rstrip(b'='))
        path.chmod(0o600)
    return path.read_text().strip()


def system_key(path):
    if not path.exists():
        path.write_text(base64.b64encode(os.urandom(32)).decode())
    return path.read_text().strip()


def local_secrets(c):
    # Directory and PF share the CA, but never share their TLS private keys.
    if not (SECRET/'ca.key').exists() or not (SECRET/'ca.crt').exists():
        raise ValueError('Run make secrets first to create the shared internal CA')
    random_file(SECRET/'federate-admin-password')
    random_file(SECRET/'federate-signing.pin')
    signing = SECRET/'federate-signing'
    for colour in ('signing', *project.COLOURS):
        name = signing if colour == 'signing' else tls_name(colour)
        random_file(name.with_suffix('.pin'))
        if not name.with_suffix('.p12').exists():
            ext = name.with_suffix('.cnf')
            sans = ['DNS:localhost', f'DNS:{c["admin_hostname"]}', f'DNS:{c["engine_hostname"]}']
            if colour != 'signing':
                sans += [f'DNS:pf-{colour}-pingfederate-admin.{namespace(colour)}.svc.cluster.local',
                         f'DNS:pf-{colour}-pingfederate-engine.{namespace(colour)}.svc.cluster.local',
                         f'DNS:*.pf-{colour}-pingfederate-cluster.{namespace(colour)}.svc.cluster.local']
            ext.write_text('subjectAltName='+','.join(sans)+'\nbasicConstraints=CA:FALSE\n'
                           'keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n')
            project.run(['openssl','req','-new','-newkey','rsa:3072','-nodes','-subj',f'/CN={c["engine_hostname"]}',
                         '-keyout',name.with_suffix('.key'),'-out',name.with_suffix('.csr')])
            project.run(['openssl','x509','-req','-in',name.with_suffix('.csr'),'-CA',SECRET/'ca.crt',
                         '-CAkey',SECRET/'ca.key','-CAcreateserial','-days','365','-extfile',ext,
                         '-out',name.with_suffix('.crt')])
            project.run(['openssl','pkcs12','-export','-name','ping','-inkey',name.with_suffix('.key'),
                         '-in',name.with_suffix('.crt'),'-certfile',SECRET/'ca.crt','-out',name.with_suffix('.p12'),
                         '-passout','env:PF_EXPORT_PIN'],
                        env={**os.environ,'PF_EXPORT_PIN':name.with_suffix('.pin').read_text().strip()})
        project.run(['keytool','-list','-keystore',name.with_suffix('.p12'),'-storetype','PKCS12',
                     '-storepass:file',name.with_suffix('.pin')],capture=True)
    mapping = json.loads((ROOT/'profiles/pingfederate/secret-variables.json').read_text())
    for colour in project.COLOURS:
        folder = SECRET/f'pf-{colour}'
        (folder/'vars').mkdir(parents=True,exist_ok=True)
        jwk = folder/'pf.jwk'
        if not jwk.exists():
            jwk.write_text(json.dumps({'keys':[{'kty':'oct','kid':str(uuid.uuid4()),
                'k':base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')}]}))
        def b64(path): return base64.b64encode(path.read_bytes()).decode()
        values = {
            'tls-p12':b64(tls_name(colour).with_suffix('.p12')),
            'tls-pin':tls_name(colour).with_suffix('.pin').read_text().strip(),
            'signing-p12':b64(SECRET/'federate-signing.p12'),
            'signing-pin':(SECRET/'federate-signing.pin').read_text().strip(),
            'ca':b64(SECRET/'ca.crt'),
            'system-current':system_key(folder/'system-current'),
            'system-pending':system_key(folder/'system-pending'),
            'admin-password':(SECRET/'federate-admin-password').read_text().strip(),
            'ldap-password':(SECRET/'federate-ldap-password').read_text().strip(),
            'demo-password':(SECRET/'demo-user-password').read_text().strip()}
        for name, kind in mapping.items():
            (folder/'vars'/name).write_text(values[kind])
        (folder/'vars/PING_IDENTITY_PASSWORD').write_text(values['admin-password'])
        (folder/'vars/PF_CLUSTER_PASSWORD').write_text(random_file(folder/'cluster-password'))
    for path in SECRET.rglob('*'):
        if path.is_file(): path.chmod(0o600)


def values(c, colour, repository=None):
    directory = project.config()
    env = {'PING_IDENTITY_ACCEPT_EULA':'YES' if directory['accept_eula'] else 'NO',
           'SERVER_PROFILE_URL':'', 'SECRETS_DIR':'/run/ping-secrets',
           'LICENSE_DIR':'/run/ping-secrets', 'ROOT_USER':'Administrator',
           'PF_ADMIN_PUBLIC_HOSTNAME':c['admin_hostname'], 'PF_ADMIN_PUBLIC_PORT_HTTPS':'443',
           'PF_ADMIN_PUBLIC_BASEURL':f'https://{c["admin_hostname"]}',
           'PF_ENGINE_PUBLIC_HOSTNAME':c['engine_hostname'], 'PF_ENGINE_PUBLIC_PORT_HTTPS':'443',
           'USER_BASE_DN':directory['base_dn'], 'PF_NODE_GROUP_ID':colour,
           'PD_ENGINE_PRIVATE_HOSTNAME':'pd-pingdirectoryproxy.identity-proxy.svc.cluster.local',
           'PD_ENGINE_PRIVATE_PORT_LDAP':'636', 'VERBOSE':'false',
           'PF_CLUSTER_ADMIN_NODES_SYNC_ENABLED':'false'}
    image = {'repositoryFqn':repository or 'example.invalid/pingfederate', 'tag':c['image_tags'][colour]}
    mapping = json.loads((ROOT/'profiles/pingfederate/secret-variables.json').read_text())
    secret_items = [{'key':key,'path':key} for key in ('ca.crt','PingFederate.lic','pf.jwk')]
    # Keep the operator's filename/Secret key, but Linux runtime expects lowercase.
    next(item for item in secret_items if item['key']=='PingFederate.lic')['path']='pingfederate.lic'
    secret_items += [{'key':'var.'+name,'path':'vars/'+name}
                     for name in (*mapping,'PING_IDENTITY_PASSWORD','PF_CLUSTER_PASSWORD')]
    v = {'global':{'image':{'tag':c['base_image_tag']},'envs':env,
        'includeInitContainers':['copy-federate-secrets','wait-for-directory-proxy'],
        'includeVolumes':['pf-projected','pf-secrets','pf-bootstrap-hooks'],
        'volumeMounts':[{'name':'pf-secrets','mountPath':'/run/ping-secrets','readOnly':True},
            {'name':'pf-bootstrap-hooks','mountPath':'/opt/in/hooks/03-build-run-plan.sh.post',
             'subPath':'03-build-run-plan.sh.post','readOnly':True}]},
        'volumes':{'pf-projected':{'secret':{'secretName':f'pf-{colour}-runtime','items':secret_items}},
                   'pf-bootstrap-hooks':{'configMap':{'name':f'pf-{colour}-bootstrap-hooks','defaultMode':493}},
                   'pf-secrets':{'emptyDir':{'medium':'Memory'}}},
        'initContainers':{'copy-federate-secrets':{
            'name':'copy-federate-secrets','image':f'pingidentity/pingtoolkit:{c["base_image_tag"]}',
            'command':['sh','-ec'], 'args':['umask 077; for source in /run/projected/*; do cp -RL "$source" /run/ping-secrets/; done; find /run/ping-secrets -mindepth 1 -type f -exec chmod 600 {} +; find /run/ping-secrets -mindepth 1 -type d -exec chmod 700 {} +'],
            'securityContext':{'runAsUser':9031,'runAsGroup':0,'allowPrivilegeEscalation':False},
            'volumeMounts':[{'name':'pf-projected','mountPath':'/run/projected','readOnly':True},
                            {'name':'pf-secrets','mountPath':'/run/ping-secrets'}]},
            'wait-for-directory-proxy':{
                'name':'wait-for-directory-proxy','image':f'pingidentity/pingtoolkit:{c["base_image_tag"]}',
                'command':['sh','-ec'],'args':[(ROOT/'scripts/wait_for_directory_proxy.sh').read_text()],
                'securityContext':{'runAsUser':9031,'runAsGroup':0,'allowPrivilegeEscalation':False},
                'volumeMounts':[{'name':'pf-secrets','mountPath':'/run/ping-secrets','readOnly':True}]}}}
    for role in ('admin','engine'):
        product = 'pingfederate-'+role
        selector = {'app.kubernetes.io/instance':f'pf-{colour}', 'app.kubernetes.io/name':product}
        v[product] = {'enabled':True,'image':image,
            'container':{'replicaCount':1 if role=='admin' else 3,
                'resources':{'requests':{'cpu':'500m','memory':'2Gi'},'limits':{'memory':'4Gi'}},
                'terminationGracePeriodSeconds':120},
            'envs':{'OPERATIONAL_MODE':'CLUSTERED_CONSOLE' if role=='admin' else 'CLUSTERED_ENGINE'}}
        if role == 'admin':
            v[product]['workload']={'type':'StatefulSet','statefulSet':{'persistentvolume':{
                'enabled':True,'volumes':{'out-dir':{'mountPath':'/opt/out','persistentVolumeClaim':{
                    'accessModes':['ReadWriteOnce'],'storageClassName':directory['storage_class'],
                    'resources':{'requests':{'storage':c['admin_storage']}}}}}}}}
        else:
            v[product]['workload']={'podDisruptionBudget':{'enabled':True,'minAvailable':2},
                'topologySpreadConstraints':[{'maxSkew':1,'topologyKey':key,'whenUnsatisfiable':'DoNotSchedule',
                    'labelSelector':{'matchLabels':selector}}
                    for key in ('topology.kubernetes.io/zone','kubernetes.io/hostname')]}
    return v


def routing(colour,c=None):
    c=c or config()
    roles=['engine']+(['admin'] if (c['admin_access_configured'] or c.get('proxy_only') is True) else [])
    # Activate the verified target first; deactivate the prior colour second.
    ordered=[colour]+[other for other in project.COLOURS if other!=colour]
    return {'apiVersion':'v1','kind':'List','items':[
        ingress(c,role,member,active=member==colour) for member in ordered for role in roles]}


def ingress(c, role, colour=None, active=True):
    colour=colour or c['initial_colour']
    if not c['origin_certificate_arn'] or not c['cloudflare_ipv4_cidrs']:
        raise ValueError('Set ACM origin certificate ARN and current Cloudflare IPv4 ranges')
    if role=='admin' and not (c['admin_access_configured'] or c.get('proxy_only') is True):
        raise ValueError('Configure Cloudflare Access or explicit proxy_only=true before enabling public admin ingress')
    return {'apiVersion':'networking.k8s.io/v1','kind':'Ingress',
        'metadata':{'name':f'pf-{role}','namespace':namespace(colour),'annotations':{
            'identity.darkedges.com/routing-state':'active' if active else 'standby',
            'alb.ingress.kubernetes.io/group.name':f'pf-{role}',
            'alb.ingress.kubernetes.io/group.order':'0' if active else '100',
            'alb.ingress.kubernetes.io/actions.pf-standby':json.dumps({'type':'fixed-response',
                'fixedResponseConfig':{'contentType':'text/plain','statusCode':'503','messageBody':'Standby colour'}}),
            'alb.ingress.kubernetes.io/scheme':'internet-facing',
            'alb.ingress.kubernetes.io/target-type':'ip',
            'alb.ingress.kubernetes.io/listen-ports':'[{"HTTPS":443}]',
            'alb.ingress.kubernetes.io/certificate-arn':c['origin_certificate_arn'],
            'alb.ingress.kubernetes.io/inbound-cidrs':','.join(c['cloudflare_ipv4_cidrs']),
            'alb.ingress.kubernetes.io/backend-protocol':'HTTPS',
            'alb.ingress.kubernetes.io/healthcheck-protocol':'HTTPS',
            'alb.ingress.kubernetes.io/healthcheck-path':'/pf/heartbeat.ping',
            'alb.ingress.kubernetes.io/success-codes':'200',
            'alb.ingress.kubernetes.io/target-group-attributes':'deregistration_delay.timeout_seconds=120,stickiness.enabled=true,stickiness.lb_cookie.duration_seconds=300'}},
        'spec':{'ingressClassName':'alb','rules':[{'host':c[f'{role}_hostname'],
            'http':{'paths':[{'path':'/','pathType':'Prefix','backend':{'service':{
                'name':f'pf-{colour}-pingfederate-{role}' if active else 'pf-standby',
                'port':({'number':9999 if role=='admin' else 9031} if active else {'name':'use-annotation'})}}}]}}]}}


def bootstrap_hooks(colour):
    return {'apiVersion':'v1','kind':'ConfigMap','metadata':{
        'name':f'pf-{colour}-bootstrap-hooks','namespace':namespace(colour)},
        'data':{'03-build-run-plan.sh.post':(ROOT/'profiles/pingfederate/hooks/03-build-run-plan.sh.post').read_text()}}


def render(c, colour, repository=None):
    project.BUILD.mkdir(exist_ok=True)
    p = project.BUILD/f'pf-{colour}.values.yaml'
    p.write_text(yaml.safe_dump(values(c,colour,repository),sort_keys=False))
    result = project.run(['helm','template',f'pf-{colour}',project.chart(),'-n',namespace(colour),
                          '--kube-version','1.35.0','-f',p],capture=True)
    result += '\n---\n'+yaml.safe_dump(bootstrap_hooks(colour),sort_keys=False)
    docs = list(yaml.safe_load_all(result))
    for doc in docs:
        if doc and doc['kind'] in ('StatefulSet','Deployment'):
            spec=doc['spec']['template']['spec']
            if len(spec.get('initContainers',[])) == 0: raise ValueError('Missing secret copy init')
    (project.BUILD/f'pf-{colour}.manifests.yaml').write_text(result)
    return p


def observed_colour():
    roles=['engine']+(['admin'] if (config()['admin_access_configured'] or config().get('proxy_only') is True) else [])
    active=[]; found=False
    for role in roles:
        selected=[]; role_found=False
        for colour in project.COLOURS:
            result=project.run(['kubectl','-n',namespace(colour),'get','ingress',f'pf-{role}',
                                '-o','json','--ignore-not-found'],capture=True)
            if result.strip():
                found=True; role_found=True
                if json.loads(result)['metadata']['annotations'].get('identity.darkedges.com/routing-state')=='active':
                    selected.append(colour)
        if len(selected)>1: raise ValueError('Both PF colours are active; retry the intended promotion')
        if role_found: active.append(selected[0] if selected else None)
    if not found: return None
    if None in active or len(set(active))!=1:
        raise ValueError('PF ingress routing differs; repair or retry the intended promotion')
    return active[0]


def ready(c, colour):
    ns=namespace(colour)
    for role in ('admin','engine'):
        data=json.loads(project.run(['kubectl','-n',ns,'get','pods','-l',
            f'app.kubernetes.io/instance=pf-{colour},app.kubernetes.io/name=pingfederate-{role}', '-o','json'],capture=True))
        expected=1 if role=='admin' else 3
        if len(data['items']) != expected:
            raise ValueError(f'{ns}: expected {expected} pf-{colour} {role} pod(s), found {len(data["items"])}. '
                             f'Check Argo CD application pf-{colour} source revision and sync status before configuring PF.')
        zones=set()
        for pod in data['items']:
            if pod['metadata'].get('deletionTimestamp') or not any(x['type']=='Ready' and x['status']=='True'
                    for x in pod.get('status',{}).get('conditions',[])):
                raise ValueError('Candidate PF pod not ready')
            if not any(x['name']==f'pingfederate-{role}' and x['image'].endswith(':'+c['image_tags'][colour])
                       for x in pod['spec']['containers']):
                raise ValueError('Candidate image mismatch')
            if role=='engine':
                node=json.loads(project.run(['kubectl','get','node',pod['spec']['nodeName'],'-o','json'],capture=True))
                zones.add(node['metadata']['labels']['topology.kubernetes.io/zone'])
        if role=='engine' and len(zones)!=3: raise ValueError('PF engines must span three AZs')


def validate_evidence(e,c,colour):
    project_config=project.config()
    if e.get('target_colour')!=colour or e.get('cluster_name')!=project_config['cluster_name'] or e.get('image_tag')!=c['image_tags'][colour]:
        raise ValueError('PF evidence target/cluster/image mismatch')
    date=dt.datetime.fromisoformat(e['verified_at'].replace('Z','+00:00'))
    if date.tzinfo is None or not 0 <= (dt.datetime.now(dt.timezone.utc)-date).total_seconds() <= 300:
        raise ValueError('PF evidence must be from the last five minutes')
    for flag in EVIDENCE_FLAGS:
        if e.get(flag) is not True: raise ValueError(f'PF verification required: {flag}')
    if not e.get('operator') or not e.get('notes'): raise ValueError('PF operator and actual verification notes required')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['validate','render','secrets','build-image','deploy','ingress','promote','status','install-controller','evidence'])
    p.add_argument('--target',choices=project.COLOURS,default='blue')
    p.add_argument('--evidence')
    p.add_argument('--notes',default='')
    p.add_argument('--attest-verified',action='store_true')
    a=p.parse_args(); c=config()
    if a.command in ('validate','render'):
        for colour in (project.COLOURS if a.command=='validate' else (a.target,)): render(c,colour)
        return
    out=project.check_context(project.config())
    if a.command=='evidence':
        ready(c,a.target)
        if not a.notes.strip(): raise ValueError('Describe actual checks and report paths with --notes')
        evidence={'target_colour':a.target,'cluster_name':project.config()['cluster_name'],
            'cluster_arn':out['cluster_arn'],'image_tag':c['image_tags'][a.target],
            'verified_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'operator':project.run(['whoami'],capture=True).strip(),'notes':a.notes,
            **{flag:a.attest_verified for flag in EVIDENCE_FLAGS}}
        project.BUILD.mkdir(exist_ok=True)
        (project.BUILD/f'pf-{a.target}-evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print('Saved PF operator attestation; promote within five minutes.' if a.attest_verified else
              'Saved draft: all verification flags remain false.')
    elif a.command=='install-controller':
        if not out.get('alb_controller_role_arn'):
            raise ValueError('Apply AWS infrastructure with enable_federate=true first')
        project.run(['helm','repo','add','eks','https://aws.github.io/eks-charts'])
        project.run(['helm','upgrade','--install','aws-load-balancer-controller','eks/aws-load-balancer-controller',
            '--version','1.16.0','-n','kube-system','--set',f'clusterName={out["cluster_name"]}',
            '--set',f'region={out["region"]}','--set',f'vpcId={out["vpc_id"]}',
            '--set','serviceAccount.name=aws-load-balancer-controller','--wait','--timeout','10m'])
    elif a.command=='secrets':
        local_secrets(c)
        license=SECRET/'PingFederate.lic'
        if not license.is_file(): raise ValueError('Supply .secrets/PingFederate.lic')
        for colour in project.COLOURS:
            project.apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace(colour)}})
            folder=SECRET/f'pf-{colour}'
            data={'ca.crt':base64.b64encode((SECRET/'ca.crt').read_bytes()).decode(),
                  'PingFederate.lic':base64.b64encode(license.read_bytes()).decode(),
                  'pf.jwk':base64.b64encode((folder/'pf.jwk').read_bytes()).decode()}
            items=[{'key':key,'path':key} for key in data]
            for file in (folder/'vars').iterdir():
                data['var.'+file.name]=base64.b64encode(file.read_bytes()).decode()
                items.append({'key':'var.'+file.name,'path':'vars/'+file.name})
            project.apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':f'pf-{colour}-runtime','namespace':namespace(colour)},'data':data})
    elif a.command=='build-image':
        repo=out['repositories']['pingfederate']; registry=repo.split('/')[0]
        password=project.run(['aws','ecr','get-login-password','--region',out['region']],capture=True)
        project.run(['docker','login','--username','AWS','--password-stdin',registry],data=password)
        project.run(['docker','build','--platform','linux/amd64','--build-arg',f'BASE_TAG={c["base_image_tag"]}',
            '-f','images/pingfederate.Dockerfile','-t',f'{repo}:{c["image_tags"][a.target]}','.'])
        project.run(['docker','push',f'{repo}:{c["image_tags"][a.target]}'])
    elif a.command=='deploy':
        if project.config().get('deployment_manager')=='argocd':
            raise ValueError('Argo CD owns PF workloads. Export and commit the candidate manifests, then sync its application.')
        if not project.config()['accept_eula']: raise ValueError('Accept Ping EULA before deployment')
        if observed_colour()==a.target:
            raise ValueError('Refusing to upgrade the active PF colour; deploy the candidate and promote')
        f=render(c,a.target,out['repositories']['pingfederate'])
        project.apply(bootstrap_hooks(a.target))
        project.run(['helm','upgrade','--install',f'pf-{a.target}',project.chart(),'-n',namespace(a.target),'-f',f,'--wait','--timeout','30m'])
    elif a.command=='ingress':
        active=observed_colour()
        ready(c,active or c['initial_colour'])
        project.apply(routing(active or c['initial_colour'],c))
    elif a.command=='promote':
        if not a.evidence: raise ValueError('Supply PF-specific promotion evidence')
        evidence=json.loads(Path(a.evidence).read_text())
        validate_evidence(evidence,c,a.target)
        if evidence.get('cluster_arn')!=out['cluster_arn']: raise ValueError('PF evidence AWS account/cluster mismatch')
        ready(c,a.target)
        project.apply(routing(a.target,c))
        print('PF grouped ingress rules updated. Wait for ALB target health/drain; retain both colours. Changes are not atomic.')
    else:
        for colour in project.COLOURS:
            project.run(['kubectl','-n',namespace(colour),'get','pods,services,ingresses','-o','wide'])
        print('Active PF colour:',observed_colour())


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,subprocess.CalledProcessError) as e:
        print(f'Error: {e}',file=project.sys.stderr); project.sys.exit(1)
