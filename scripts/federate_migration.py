#!/usr/bin/env python3
"""Read-only PF namespace migration inventory and restored-volume binding plan."""
import argparse
import copy
import json
import re
import yaml
import project
import federate


def restored_claims(pv,pvc,colour,volume_id):
    if not re.fullmatch(r'vol-[0-9a-f]+',volume_id): raise ValueError('Invalid restored volume ID')
    name=f'pf-{colour}-restored-admin'
    pv_spec=copy.deepcopy(pv['spec'])
    pv_spec['persistentVolumeReclaimPolicy']='Retain'
    pv_spec['csi']['volumeHandle']=volume_id
    pv_spec['claimRef']={'namespace':federate.namespace(colour),'name':pvc['metadata']['name']}
    claim_spec=copy.deepcopy(pvc['spec']); claim_spec['volumeName']=name
    return [
        {'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':name},'spec':pv_spec},
        {'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{
            'name':pvc['metadata']['name'],'namespace':federate.namespace(colour),
            'annotations':{'argocd.argoproj.io/sync-options':'Prune=false,Delete=false'}},'spec':claim_spec}]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',choices=project.COLOURS,required=True)
    parser.add_argument('--restored-volume-id')
    args=parser.parse_args(); out=project.check_context(project.config())
    old=federate.LEGACY_NS; sts=f'pf-{args.target}-pingfederate-admin'
    state=json.loads(project.run(['kubectl','-n',old,'get','statefulset',sts,'-o','json'],capture=True))
    templates=state['spec'].get('volumeClaimTemplates',[])
    if len(templates)!=1 or templates[0]['metadata']['name']!='out-dir':
        raise ValueError('Unexpected admin volume layout; do not guess the data volume')
    claim_name=f'out-dir-{sts}-0'
    pvc=json.loads(project.run(['kubectl','-n',old,'get','pvc',claim_name,'-o','json'],capture=True))
    pv=json.loads(project.run(['kubectl','get','pv',pvc['spec']['volumeName'],'-o','json'],capture=True))
    source=pv['spec']['csi']['volumeHandle']
    def aws(resource,identifier):
        result=json.loads(project.run(['aws','ec2',f'describe-{resource}','--'+resource[:-1]+'-ids',identifier,
            '--region',out['region'],'--output','json'],capture=True))
        return result[resource.capitalize()][0]
    volume=aws('volumes',source)
    report={'cluster_arn':out['cluster_arn'],'colour':args.target,'old_namespace':old,
            'new_namespace':federate.namespace(args.target),'statefulset':sts,'claim_name':claim_name,
            'source_volume_id':source,'availability_zone':volume['AvailabilityZone'],'region':out['region']}
    project.BUILD.mkdir(exist_ok=True)
    (project.BUILD/f'pf-{args.target}-migration.json').write_text(json.dumps(report,indent=2)+'\n')
    if args.restored_volume_id:
        if args.restored_volume_id==source: raise ValueError('Never bind the original volume into two namespaces')
        restored=aws('volumes',args.restored_volume_id)
        snapshot=aws('snapshots',restored['SnapshotId'])
        if (snapshot['VolumeId']!=source or snapshot['State']!='completed' or
            restored['AvailabilityZone']!=volume['AvailabilityZone'] or restored['Attachments'] or
            restored['State']!='available' or restored['Encrypted']!=volume['Encrypted']):
            raise ValueError('Restored disk must be an unattached, same-AZ, completed snapshot copy of the original, preserving encryption')
        manifest=project.BUILD/f'pf-{args.target}-restored-claims.yaml'
        manifest.write_text(yaml.safe_dump_all(restored_claims(pv,pvc,args.target,args.restored_volume_id),sort_keys=False))
        print(f'Prepared {manifest}; nothing applied. Bind this PVC before syncing the new admin.')
    else:
        print(json.dumps(report,indent=2))
        print('Read-only inventory saved. Follow docs/FEDERATE-NAMESPACE-MIGRATION.md; no pods or volumes changed.')


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,KeyError,project.subprocess.CalledProcessError) as error:
        print(f'Error: {error}',file=project.sys.stderr); project.sys.exit(1)
