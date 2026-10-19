#!/usr/bin/env python3
"""Argo CD bootstrap and reviewable, secret-free workload export."""
import argparse
import base64
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import yaml
import project
import federate
import access

ROOT=project.ROOT
GITOPS=ROOT/'gitops'
CHART=ROOT/'vendor/argo-cd-10.4.1.tgz'
CHART_SHA='6ccb3ee39efdf1c3bbc1e69b1a5ffdf172e52fac6873d3c2a0bb8dad40ba5af0'
NAMESPACE='argocd'
APPS={'pd-blue':'identity-blue','pd-green':'identity-green','pd-proxy':'identity-proxy',
      'pf-blue':'identity-federate-blue','pf-green':'identity-federate-green',
      'pa-blue':'identity-access-blue','pa-green':'identity-access-green'}


def config():
    path=GITOPS/'config.json'
    c=json.loads((path if path.exists() else GITOPS/'config.example.json').read_text())
    # Version-pinned recovery bundles may contain the former canonical URL.
    # Normalize only this project's rename; unrelated repositories are unchanged.
    if c['repo_url'].rstrip('/') in ('https://github.com/DarkEdgesAU/pingdirectory-eks-spot',
                                    'https://github.com/DarkEdgesAU/pingdirectory-eks-spot.git'):
        c['repo_url']='https://github.com/DarkEdgesAU/ping-eks-spot.git'
    if not c['repo_url'].startswith('https://github.com/') or '@' in c['repo_url']:
        raise ValueError('Use a credential-free GitHub repository URL; install private repo credentials separately')
    if not c['revision'] or not c['ecr_prefix'] or not c['hostname']:
        raise ValueError('Git revision, ECR prefix and hostname are required')
    for cidr in c['cloudflare_ipv4_cidrs']:
        network=ipaddress.ip_network(cidr)
        if network.version!=4 or network.prefixlen==0:
            raise ValueError('Use explicit Cloudflare IPv4 source ranges')
    return c


def chart():
    if hashlib.sha256(CHART.read_bytes()).hexdigest()!=CHART_SHA:
        raise ValueError('Argo CD chart checksum mismatch')
    return CHART


def argo_values(c, public=False):
    v={'global':{'domain':c['hostname']},
       'configs':{'cm':{'url':f'https://{c["hostname"]}',
                       'application.resourceTrackingMethod':'annotation',
                       'exec.enabled':'false','users.anonymous.enabled':'false'},
                  'params':{'server.insecure':False,
                            'server.repo.server.strict.tls':True,
                            'controller.repo.server.strict.tls':True},
                  'rbac':{'policy.default':'','policy.csv':''}},
       'dex':{'enabled':False},'notifications':{'enabled':False},
       'applicationSet':{'enabled':False},
       'server':{'certificateSecret':{'enabled':False},'ingress':{'enabled':False}}}
    if public:
        if not (c['access_policy_configured'] or c.get('proxy_only') is True) or not c['origin_certificate_arn'] or not c['cloudflare_ipv4_cidrs']:
            raise ValueError('Configure Cloudflare Access or explicit proxy_only=true, origin ACM certificate and Cloudflare source CIDRs before public Argo ingress')
        v['server']['ingress']={
            'enabled':True,'controller':'generic','ingressClassName':'alb','hostname':c['hostname'],
            'annotations':{
                'alb.ingress.kubernetes.io/scheme':'internet-facing',
                'alb.ingress.kubernetes.io/target-type':'ip',
                'alb.ingress.kubernetes.io/listen-ports':'[{"HTTPS":443}]',
                'alb.ingress.kubernetes.io/certificate-arn':c['origin_certificate_arn'],
                'alb.ingress.kubernetes.io/inbound-cidrs':','.join(c['cloudflare_ipv4_cidrs']),
                'alb.ingress.kubernetes.io/backend-protocol':'HTTPS',
                'alb.ingress.kubernetes.io/healthcheck-protocol':'HTTPS',
                'alb.ingress.kubernetes.io/healthcheck-path':'/healthz',
                'alb.ingress.kubernetes.io/success-codes':'200'}}
    return v


def argo_render(c, public=False):
    project.BUILD.mkdir(exist_ok=True)
    path=project.BUILD/'argocd.values.yaml'
    path.write_text(yaml.safe_dump(argo_values(c,public),sort_keys=False))
    result=project.run(['helm','template','argocd',chart(),'-n',NAMESPACE,
                        '--kube-version','1.35.0','-f',path],capture=True)
    list(yaml.safe_load_all(result))
    (project.BUILD/'argocd.manifests.yaml').write_text(result)
    return path


def application(c,name,namespace):
    return {'apiVersion':'argoproj.io/v1alpha1','kind':'Application',
        'metadata':{'name':name,'namespace':NAMESPACE}, # No cascading deletion finalizer.
        'spec':{'project':'ping-workloads',
            'source':{'repoURL':c['repo_url'],'targetRevision':c['revision'],
                      'path':f'gitops/workloads/{name}','directory':{'recurse':False}},
            'destination':{'server':'https://kubernetes.default.svc','namespace':namespace},
            'syncPolicy':{'syncOptions':['RespectIgnoreDifferences=true','FailOnSharedResource=true']},
            # Protect existing retained replica counts from a Git edit or sync.
            'ignoreDifferences':[{'group':'apps','kind':'StatefulSet','jsonPointers':['/spec/replicas']}]}}


def app_project(c):
    return {'apiVersion':'argoproj.io/v1alpha1','kind':'AppProject',
        'metadata':{'name':'ping-workloads','namespace':NAMESPACE},
        'spec':{'description':'Manual Ping workload deployments; runtime routing and secrets are operator-owned',
            'sourceRepos':[c['repo_url']],
            'destinations':[{'server':'https://kubernetes.default.svc','namespace':ns} for ns in sorted(set(APPS.values()))],
            'clusterResourceWhitelist':[],
            'namespaceResourceWhitelist':[
                {'group':group,'kind':kind} for group,kind in (
                    ('','ConfigMap'),('','Service'),('','ServiceAccount'),('','PersistentVolumeClaim'),
                    ('apps','Deployment'),('apps','StatefulSet'),('policy','PodDisruptionBudget'),
                    ('networking.k8s.io','Ingress'),('networking.k8s.io','NetworkPolicy'),
                    ('rbac.authorization.k8s.io','Role'),('rbac.authorization.k8s.io','RoleBinding'))]}}


def protect(doc):
    if doc['kind']=='Secret': raise ValueError('Secret manifests may never enter GitOps exports')
    if doc['metadata']['name'] in ('pd-routing','pf-active-admin','pf-active-engine','pa-active-admin','pa-active-engine'):
        raise ValueError('Runtime routing must not be reconciled by Argo CD')
    if doc['kind'] in ('StatefulSet','PersistentVolumeClaim'):
        annotations=doc['metadata'].get('annotations') or {}
        doc['metadata']['annotations']=annotations
        annotations['argocd.argoproj.io/sync-options']='Prune=false,Delete=false'
    if doc['kind']=='StatefulSet':
        spec=doc['spec']
        if spec.get('persistentVolumeClaimRetentionPolicy',{}).get('whenDeleted','Retain')!='Retain':
            raise ValueError('StatefulSet PVC deletion is prohibited')
        spec['persistentVolumeClaimRetentionPolicy']={'whenDeleted':'Retain','whenScaled':'Retain'}
    return doc


def export(c):
    directory=project.config(); pf=federate.config()
    repos={product:f'{c["ecr_prefix"]}/{product}' for product in ('pingdirectory','pingdirectoryproxy','pingfederate','pingaccess')}
    if not c['ecr_prefix'].endswith('/'+directory['cluster_name']):
        raise ValueError('ECR prefix must match the configured cluster')
    for name,ns in APPS.items():
        colour=name.removeprefix('pd-').removeprefix('pf-').removeprefix('pa-')
        if name.startswith('pd-'):
            project.render(directory,colour,repos)
            source=project.BUILD/f'{colour}.manifests.yaml'
        elif name.startswith('pf-'):
            federate.render(pf,colour,repos['pingfederate'])
            source=project.BUILD/f'pf-{colour}.manifests.yaml'
        else:
            access.render(access.config(),colour,repos['pingaccess'])
            source=project.BUILD/f'pa-{colour}.manifests.yaml'
        docs=[protect(doc) for doc in yaml.safe_load_all(source.read_text()) if doc]
        folder=GITOPS/'workloads'/name
        folder.mkdir(parents=True,exist_ok=True)
        output=folder/'resources.yaml'
        rendered=yaml.safe_dump_all(docs,sort_keys=False)
        if not output.exists() or output.read_text()!=rendered:
            output.write_text(rendered,newline='\n')
    apps=GITOPS/'applications'
    apps.mkdir(exist_ok=True)
    project_file=apps/'project.yaml'
    project_yaml=yaml.safe_dump(app_project(c),sort_keys=False)
    if not project_file.exists() or project_file.read_text()!=project_yaml:
        project_file.write_text(project_yaml,newline='\n')
    for name,ns in APPS.items():
        application_file=apps/f'{name}.yaml'
        application_yaml=yaml.safe_dump(application(c,name,ns),sort_keys=False)
        if not application_file.exists() or application_file.read_text()!=application_yaml:
            application_file.write_text(application_yaml,newline='\n')
    import observability
    observability.export()
    logging_application(c)
    import platform_services
    platform_services.export()
    print(f'Exported {len(APPS)+4} secret-free applications. Review and commit gitops/ before syncing; nothing was deployed.')


def logging_application(c):
    # Separate namespace-scoped project; no cluster RBAC or Secrets managed by Git.
    spec=app_project(c)['spec']
    spec['description']='OpenSearch log-shipping POC'
    spec['destinations']=[{'server':'https://kubernetes.default.svc','namespace':'logging'}]
    spec['namespaceResourceWhitelist'].append({'group':'apps','kind':'DaemonSet'})
    spec['namespaceResourceWhitelist'].append({'group':'batch','kind':'Job'})
    project_doc={'apiVersion':'argoproj.io/v1alpha1','kind':'AppProject',
        'metadata':{'name':'observability','namespace':NAMESPACE},'spec':spec}
    app=application(c,'logging','logging'); app['spec']['project']='observability'
    for name,doc in [('logging-project.yaml',project_doc),('logging.yaml',app)]:
        (GITOPS/'applications'/name).write_text(yaml.safe_dump(doc,sort_keys=False),newline='\n')


def validate(c):
    argo_render(c)
    for path in (GITOPS/'workloads').glob('*/resources.yaml'):
        for doc in yaml.safe_load_all(path.read_text()):
            if not doc: continue
            if protect(copy.deepcopy(doc)) != doc:
                raise ValueError(f'Missing GitOps data-retention guards in {path}')
            if doc['kind']=='StatefulSet' and doc['metadata']['name']=='pd-pingdirectory' and doc['spec']['replicas']!=3:
                raise ValueError('Directory replica count must remain three')
    for name,ns in APPS.items():
        actual=GITOPS/'applications'/f'{name}.yaml'
        if actual.exists() and yaml.safe_load(actual.read_text())!=application(c,name,ns):
            raise ValueError('Application configuration drift; rerun make gitops-export')


def secrets(c):
    from vault_runtime import generator_guard
    generator_guard()
    folder=ROOT/'.secrets/argocd'
    ca=ROOT/'.secrets'
    if not (ca/'ca.key').exists() or not (ca/'ca.crt').exists():
        raise ValueError('Create the common internal CA with make secrets first')
    folder.mkdir(parents=True,exist_ok=True)
    project.apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':NAMESPACE}})
    for name,sans in (
        ('argocd-server-tls',[c['hostname'],'localhost','argocd-server','argocd-server.argocd.svc','argocd-server.argocd.svc.cluster.local']),
        ('argocd-repo-server-tls',['argocd-repo-server','argocd-repo-server.argocd.svc','argocd-repo-server.argocd.svc.cluster.local'])):
        key=folder/f'{name}.key'; crt=folder/f'{name}.crt'
        if not crt.exists():
            ext=folder/f'{name}.cnf'
            ext.write_text('subjectAltName='+','.join('DNS:'+san for san in sans)+'\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n')
            project.run(['openssl','req','-new','-newkey','rsa:3072','-nodes','-subj',f'/CN={sans[0]}','-keyout',key,'-out',folder/f'{name}.csr'])
            project.run(['openssl','x509','-req','-in',folder/f'{name}.csr','-CA',ca/'ca.crt','-CAkey',ca/'ca.key',
                '-CAcreateserial','-days','365','-extfile',ext,'-out',crt])
        key.chmod(0o600)
        project.apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':NAMESPACE},
            'type':'kubernetes.io/tls','data':{
                'tls.key':base64.b64encode(key.read_bytes()).decode(),
                'tls.crt':base64.b64encode(crt.read_bytes()+(ca/'ca.crt').read_bytes()).decode(),
                'ca.crt':base64.b64encode((ca/'ca.crt').read_bytes()).decode()}})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['validate','render','export','secrets','bootstrap','applications'])
    p.add_argument('--public',action='store_true')
    a=p.parse_args(); c=config()
    if a.command=='validate': validate(c); return
    if a.command=='export': export(c); return
    if a.command=='render': argo_render(c,a.public); return
    project.check_context(project.config())
    if a.command=='secrets': secrets(c)
    elif a.command=='bootstrap':
        values=argo_render(c,a.public)
        project.run(['helm','upgrade','--install','argocd',chart(),'-n',NAMESPACE,'--create-namespace',
                     '-f',values,'--wait','--timeout','15m'])
    else:
        validate(c)
        if not (GITOPS/'applications/project.yaml').is_file(): raise ValueError('Run gitops-export, review and commit first')
        # Activate a guard before handing resources to Argo. Do not uninstall
        # old Helm releases: that would delete their running workloads.
        config_path=ROOT/'config.json'
        settings=project.config(); settings['deployment_manager']='argocd'
        config_path.write_text(json.dumps(settings,indent=2)+'\n')
        project.run(['kubectl','apply','-f',GITOPS/'applications/project.yaml'])
        for name in APPS:
            project.run(['kubectl','apply','-f',GITOPS/'applications'/f'{name}.yaml'])
        for name in ('logging-project.yaml','logging.yaml'):
            project.run(['kubectl','apply','-f',GITOPS/'applications'/name])
        for name in ('metrics','delegator','pingcentral'):
            for suffix in ('-project.yaml','.yaml'):
                project.run(['kubectl','apply','-f',GITOPS/'applications'/(name+suffix)])
        print('Applications registered without sync. Direct workload Helm deployment is now disabled locally.')


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,subprocess.CalledProcessError) as e:
        print(f'Error: {e}',file=project.sys.stderr); project.sys.exit(1)
