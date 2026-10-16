#!/usr/bin/env python3
"""Independent PingAccess colours; rendering is local, routing is operator-owned."""
import argparse
import base64
import copy
import datetime as dt
import ipaddress
import json
from pathlib import Path
import re
import yaml
import project
import federate

ROOT=project.ROOT
FLAGS=('configuration_verified','internal_tls_verified','login_verified',
       'session_compatibility_verified','rollback_verified')


def config():
    path=ROOT/'access.json'
    c=json.loads((path if path.exists() else ROOT/'access.example.json').read_text())
    if c['engine_replicas']!=3 or c['initial_colour'] not in project.COLOURS:
        raise ValueError('PingAccess requires three engines and a blue/green initial colour')
    for key in ('admin_hostname','engine_hostname'):
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+',c[key]): raise ValueError('Invalid PA hostname')
    for cidr in c['cloudflare_ipv4_cidrs']:
        net=ipaddress.ip_network(cidr)
        if net.version!=4 or net.prefixlen==0: raise ValueError('Explicit Cloudflare IPv4 ranges required')
    for tag in c['image_tags'].values():
        if not tag or tag=='latest': raise ValueError('Use immutable PA image tags')
    return c


def namespace(colour):
    if colour not in project.COLOURS: raise ValueError('Invalid colour')
    return f'identity-access-{colour}'


def values(c,colour,repository=None):
    # Reuse chart shape/scheduling, never PF topology, files or secrets.
    v=federate.values({**c,'product_version':'unused'},colour,repository or 'example.invalid/pingaccess')
    env={'PING_IDENTITY_ACCEPT_EULA':'YES' if project.config()['accept_eula'] else 'NO',
         'ROOT_USER':'administrator','PA_ADMIN_PUBLIC_HOSTNAME':federate.admin_hostname(c,colour),
         'PA_ENGINE_PUBLIC_HOSTNAME':c['engine_hostname'],'VERBOSE':'false','PING_DEBUG':'false'}
    v['global']['envs']=env
    v['global']['includeInitContainers']=['copy-access-secrets']
    v['global']['includeVolumes']=['pa-projected','pa-secrets']
    v['global']['volumeMounts']=[{'name':'pa-secrets','mountPath':'/run/ping-secrets','readOnly':True}]
    v['volumes']={'pa-projected':{'secret':{'secretName':f'pa-{colour}-runtime','items':[
        {'key':'admin-password','path':'admin-password'},{'key':'PingAccess.lic','path':'pingaccess.lic'},
        {'key':'ca.crt','path':'ca.crt'}]}},'pa-secrets':{'emptyDir':{'medium':'Memory'}}}
    init=copy.deepcopy(v['initContainers']['copy-federate-secrets'])
    init['name']='copy-access-secrets'
    init['volumeMounts']=[{'name':'pa-projected','mountPath':'/run/projected','readOnly':True},
                         {'name':'pa-secrets','mountPath':'/run/ping-secrets'}]
    v['initContainers']={'copy-access-secrets':init}
    for role in ('admin','engine'):
        product=v.pop('pingfederate-'+role)
        product['envs']={'OPERATIONAL_MODE':'CLUSTERED_CONSOLE' if role=='admin' else 'CLUSTERED_ENGINE',
                        'PA_RUN_PA_OPERATIONAL_MODE':'CLUSTERED_CONSOLE' if role=='admin' else 'CLUSTERED_ENGINE'}
        if role=='engine':
            for constraint in product['workload']['topologySpreadConstraints']:
                constraint['labelSelector']['matchLabels']={
                    'app.kubernetes.io/instance':f'pa-{colour}','app.kubernetes.io/name':'pingaccess-engine'}
        v['pingaccess-'+role]=product
    return v


def render(c,colour,repository=None):
    project.BUILD.mkdir(exist_ok=True)
    path=project.BUILD/f'pa-{colour}.values.yaml'
    path.write_text(yaml.safe_dump(values(c,colour,repository),sort_keys=False))
    result=project.run(['helm','template',f'pa-{colour}',project.chart(),'-n',namespace(colour),
                        '--kube-version','1.35.0','-f',path],capture=True)
    docs=[x for x in yaml.safe_load_all(result) if x]
    for doc in docs:
        doc['metadata']['namespace']=namespace(colour)
        if doc['kind']=='Secret': raise ValueError('PA rendering must be secret-free')
        if doc['kind']=='StatefulSet':
            doc['spec']['persistentVolumeClaimRetentionPolicy']={'whenDeleted':'Retain','whenScaled':'Retain'}
    import observability
    docs=observability.attach(docs)
    import platform_services
    docs=platform_services.attach(docs)
    (project.BUILD/f'pa-{colour}.manifests.yaml').write_text(yaml.safe_dump_all(docs,sort_keys=False))
    return path


def ingress(c,role,colour,active):
    doc=federate.ingress(c,role,colour,active)
    doc['metadata']['namespace']=namespace(colour)
    doc['metadata']['name']='pa-'+role
    a=doc['metadata']['annotations']
    a['alb.ingress.kubernetes.io/group.name']='pa-'+role
    a['alb.ingress.kubernetes.io/actions.pa-standby']=a.pop('alb.ingress.kubernetes.io/actions.pf-standby')
    a['alb.ingress.kubernetes.io/healthcheck-path']='/pa/heartbeat.ping'
    for rule in doc['spec']['rules']:
        service=rule['http']['paths'][0]['backend']['service']
        if service['name']=='pf-standby': service['name']='pa-standby'
        else:
            service['name']=f'pa-{colour}-pingaccess-{role}'
            service['port']={'number':9000 if role=='admin' else 3000}
    return doc


def observed_colour(c):
    observed=[]
    for role in ['engine']+(['admin'] if c.get('proxy_only') or c['admin_access_configured'] else []):
        active=[]; found=False
        for colour in project.COLOURS:
            result=project.run(['kubectl','-n',namespace(colour),'get','ingress','pa-'+role,
                                '-o','json','--ignore-not-found'],capture=True)
            if result.strip():
                found=True
                if json.loads(result)['metadata']['annotations'].get('identity.darkedges.com/routing-state')=='active': active.append(colour)
        if found:
            if len(active)!=1: raise ValueError('Ambiguous PA routing; refusing changes')
            observed.append(active[0])
    if len(set(observed))>1: raise ValueError('PA admin/engine routes disagree')
    return observed[0] if observed else None


def routing(c,colour):
    roles=['engine']+(['admin'] if c.get('proxy_only') or c['admin_access_configured'] else [])
    return {'apiVersion':'v1','kind':'List','items':[
        ingress(c,role,member,member==colour) for member in (colour,*[x for x in project.COLOURS if x!=colour]) for role in roles]}


def validate_evidence(e,c,colour):
    out=project.tf_outputs()
    if (e.get('target_colour')!=colour or e.get('image_tag')!=c['image_tags'][colour]
            or e.get('cluster_arn')!=out['cluster_arn']): raise ValueError('PA evidence identity mismatch')
    date=dt.datetime.fromisoformat(e['verified_at'].replace('Z','+00:00'))
    if date.tzinfo is None or not 0<=(dt.datetime.now(dt.timezone.utc)-date).total_seconds()<=300:
        raise ValueError('PA evidence must be within five minutes')
    if not e.get('operator') or not e.get('notes') or any(e.get(flag) is not True for flag in FLAGS):
        raise ValueError('PA promotion requires every verification attestation')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['render','validate','secrets','build-image','ingress','promote','status'])
    parser.add_argument('--target',choices=project.COLOURS,default='blue')
    parser.add_argument('--evidence',type=Path)
    a=parser.parse_args(); c=config()
    if a.command in ('render','validate'):
        for colour in project.COLOURS if a.command=='validate' else (a.target,): render(c,colour)
        return
    out=project.check_context(project.config())
    if a.command=='secrets':
        for path in (ROOT/'.secrets/ca.crt',ROOT/'.secrets/PingAccess.lic'):
            if not path.is_file(): raise ValueError('Supply '+str(path))
        for colour in project.COLOURS:
            password=ROOT/f'.secrets/pa-{colour}/admin-password'
            password.parent.mkdir(parents=True,exist_ok=True)
            federate.random_file(password)
            project.apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace(colour)}})
            project.apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':f'pa-{colour}-runtime','namespace':namespace(colour)},
                'data':{key:base64.b64encode(path.read_bytes()).decode() for key,path in {
                    'admin-password':password,'PingAccess.lic':ROOT/'.secrets/PingAccess.lic','ca.crt':ROOT/'.secrets/ca.crt'}.items()}})
    elif a.command=='build-image':
        repo=out['repositories'].get('pingaccess')
        if not repo: raise ValueError('Apply the reviewed Terraform ECR addition first')
        password=project.run(['aws','ecr','get-login-password','--region',out['region']],capture=True)
        project.run(['docker','login','--username','AWS','--password-stdin',repo.split('/')[0]],data=password)
        tag=repo+':'+c['image_tags'][a.target]
        project.run(['docker','build','--platform','linux/amd64','--build-arg','BASE_TAG='+c['base_image_tag'],
                     '-f','images/pingaccess.Dockerfile','-t',tag,'.'])
        project.run(['docker','push',tag])
    elif a.command in ('ingress','promote'):
        if a.command=='promote':
            if not a.evidence: raise ValueError('Supply reviewed PA verification evidence')
            validate_evidence(json.loads(a.evidence.read_text()),c,a.target)
            for role in ('admin','engine'):
                pods=json.loads(project.run(['kubectl','-n',namespace(a.target),'get','pods','-l',
                    'app.kubernetes.io/name=pingaccess-'+role,'-o','json'],capture=True))['items']
                if len(pods)!=(1 if role=='admin' else 3): raise ValueError('Unexpected PA pod count')
                for pod in pods:
                    if pod['metadata'].get('deletionTimestamp') or not any(
                            x['type']=='Ready' and x['status']=='True' for x in pod.get('status',{}).get('conditions',[])):
                        raise ValueError('PA pod is not ready')
                    containers=[x for x in pod['spec']['containers'] if x['name']=='pingaccess-'+role]
                    if len(containers)!=1 or not containers[0]['image'].endswith(':'+c['image_tags'][a.target]):
                        raise ValueError('PA pod image does not match verified tag')
                if role=='engine':
                    zones={json.loads(project.run(['kubectl','get','node',p['spec']['nodeName'],'-o','json'],capture=True))['metadata']['labels']['topology.kubernetes.io/zone'] for p in pods}
                    if len(zones)!=3: raise ValueError('PA engines must span three AZs')
            colour=a.target
        else: colour=observed_colour(c) or c['initial_colour']
        project.apply(routing(c,colour))
        print('PA route rules configured; verify ALB convergence, drain and authentication before declaring cutover complete.')
    else:
        for colour in project.COLOURS: project.run(['kubectl','-n',namespace(colour),'get','pods,pvc'])
        print('Active PingAccess colour:',observed_colour(c))


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,project.subprocess.CalledProcessError) as error: raise SystemExit(str(error))
