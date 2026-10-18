#!/usr/bin/env python3
"""Prepare recovery manifests from a teardown inventory. No cloud/Kubernetes calls."""
import argparse
import copy
import json
from pathlib import Path
import yaml

CORE_NAMESPACES=('identity-blue','identity-green','identity-proxy',
                 'identity-federate-blue','identity-federate-green')
WORKLOAD_NAMESPACES=(*CORE_NAMESPACES,'identity-access-blue','identity-access-green',
                     'identity-central','identity-delegator','logging','monitoring')


def clean_secret(resource):
    # Helm release records and service-account tokens are not portable backups.
    if resource.get('type') in ('helm.sh/release.v1','kubernetes.io/service-account-token'):
        return None
    if resource['metadata']['namespace']=='argocd' and resource['metadata']['name'] in (
            'argocd-secret','argocd-initial-admin-secret','argocd-redis','argocd-server-tls','argocd-repo-server-tls'):
        return None  # bootstrap a fresh Argo installation; TLS comes from argocd-secrets.
    return {'apiVersion':'v1','kind':'Secret','metadata':{
        'name':resource['metadata']['name'],'namespace':resource['metadata']['namespace'],
        'labels':copy.deepcopy(resource['metadata'].get('labels',{}))},
        'type':resource.get('type','Opaque'),'data':copy.deepcopy(resource.get('data',{}))}


def binding(pv,pvc):
    spec=copy.deepcopy(pv['spec'])
    if spec.get('csi',{}).get('driver')!='ebs.csi.aws.com': raise ValueError('Unexpected volume driver')
    if pvc['spec'].get('volumeName')!=pv['metadata']['name']: raise ValueError('PV/PVC inventory mismatch')
    claim={'namespace':pvc['metadata']['namespace'],'name':pvc['metadata']['name']}
    if any(spec.get('claimRef',{}).get(k)!=v for k,v in claim.items()): raise ValueError('Claim identity mismatch')
    spec['claimRef']=claim
    spec['persistentVolumeReclaimPolicy']='Retain'
    claim_spec=copy.deepcopy(pvc['spec'])
    metadata={'name':pv['metadata']['name']}
    return [
        {'apiVersion':'v1','kind':'PersistentVolume','metadata':metadata,'spec':spec},
        {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{
            **claim,'annotations':{'argocd.argoproj.io/sync-options':'Prune=false,Delete=false'}},'spec':claim_spec}]


def prepare(backup):
    inventories={'persistent-volumes.json':json.loads((backup/'persistent-volumes.json').read_text())}
    for namespace in (*WORKLOAD_NAMESPACES,'argocd'):
        source=backup/f'{namespace}-resources.json'
        if source.is_file(): inventories[source.name]=json.loads(source.read_text())
    return prepare_inventory(inventories)


def prepare_inventory(inventories):
    """Prepare in memory so runner secret delivery needs no plaintext temp files."""
    pvs=inventories['persistent-volumes.json']['items']
    namespaces=[]; secrets=[]; runtime=[]; claims=[]; volumes=[]
    for namespace in (*WORKLOAD_NAMESPACES,'argocd'):
        source=inventories.get(f'{namespace}-resources.json')
        if source is None:
            # Older backups predate optional services. But a saved PV belonging
            # to one proves its inventory is required for safe recovery.
            retained=any(pv.get('spec',{}).get('claimRef',{}).get('namespace')==namespace for pv in pvs)
            if namespace in (*CORE_NAMESPACES,'argocd') or retained:
                raise ValueError('Missing namespace inventory: '+namespace)
            continue
        namespaces.append({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})
        resources=source['items']
        for resource in resources:
            if resource['kind']=='Secret':
                cleaned=clean_secret(resource)
                if cleaned: secrets.append(cleaned)
            elif resource['kind']=='ConfigMap' and resource['metadata']['name'] in ('pd-routing','argocd-tls-certs-cm'):
                runtime.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{
                    'name':resource['metadata']['name'],'namespace':namespace},'data':copy.deepcopy(resource.get('data',{}))})
            elif resource['kind']=='PersistentVolumeClaim':
                matches=[pv for pv in pvs if pv['metadata']['name']==resource['spec'].get('volumeName')]
                if len(matches)!=1: raise ValueError('Missing or ambiguous saved PV')
                claims.extend(binding(matches[0],resource))
                volumes.append(matches[0]['spec']['csi']['volumeHandle'])
    return {'00-namespaces.yaml':namespaces,'20-runtime-secrets.yaml':secrets,
            '30-retained-claims.yaml':claims,'40-runtime-configmaps.yaml':runtime},volumes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup',type=Path,required=True)
    a=parser.parse_args()
    documents,volumes=prepare(a.backup)
    folder=a.backup/'prepared'; folder.mkdir(mode=0o700,exist_ok=True)
    for name,items in documents.items():
        path=folder/name; path.write_text(yaml.safe_dump_all(items,sort_keys=False)); path.chmod(0o600)
    (folder/'volume-ids.json').write_text(json.dumps(volumes,indent=2)+'\n')
    print(f'Prepared {len(volumes)} retained bindings in {folder}. Nothing applied.')
    print('Secret manifests are private. Legacy identity-federate volumes are intentionally excluded.')


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,KeyError) as error: raise SystemExit(str(error))
