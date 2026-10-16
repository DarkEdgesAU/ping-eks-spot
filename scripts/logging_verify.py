#!/usr/bin/env python3
"""Emit file probes on each live Ping pod and verify they arrive in OpenSearch."""
import base64
import json
import ssl
import subprocess
import time
import urllib.request
import urllib.error
import uuid
import observability as obs
import project


def main():
    project.check_context(project.config())
    marker='shipping-'+uuid.uuid4().hex
    namespaces=('identity-blue','identity-green','identity-proxy','identity-federate-blue',
                'identity-federate-green','identity-access-blue','identity-access-green')
    expected=[]
    for ns in namespaces:
        pods=json.loads(project.run(['kubectl','-n',ns,'get','pods','-o','json'],capture=True))['items']
        for pod in pods:
            main=[x for x in pod['spec']['containers'] if x['name'].startswith(('pingdirectory','pingfederate','pingaccess'))]
            if not main: continue
            if not any(x['name']=='log-shipper' for x in pod['spec']['containers']):
                raise ValueError(f'{ns}/{pod["metadata"]["name"]}: log sidecar missing; sync the new workload')
            name=pod['metadata']['name']; product=main[0]['name']
            path='/opt/out/instance/'+('logs' if product.startswith('pingdirectory') else 'log')+'/logging-poc.log'
            value=marker+'-'+name
            project.run(['kubectl','-n',ns,'exec',name,'-c',product,'--','sh','-ec',
                'printf "%s\\n" "$1" >> "$2"','probe',value,path])
            expected.append((ns,name,value))
    if not expected: raise ValueError('No Ping workloads found')
    log=project.BUILD/'logging-verify-forward.log'
    with log.open('w') as stream:
        process=subprocess.Popen(['kubectl','-n','logging','port-forward','--address','127.0.0.1',
            'service/opensearch','19200:9200'],stdout=stream,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+20
            while 'Forwarding from' not in log.read_text():
                if process.poll() is not None or time.monotonic()>deadline:
                    raise ValueError('OpenSearch tunnel unavailable; stop local POC first if port 19200 is busy')
                time.sleep(.2)
            # Force IPv4 while retaining certificate SAN/hostname validation.
            import socket
            from unittest.mock import patch
            lookup=socket.getaddrinfo
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=str(obs.PRIVATE/'ca.crt'))))
            token=base64.b64encode(('admin:'+(obs.PRIVATE/'admin-password').read_text()).encode()).decode()
            def request(url,data):
                req=urllib.request.Request(url,data=json.dumps(data).encode(),headers={
                    'Authorization':'Basic '+token,'Content-Type':'application/json','osd-xsrf':'true'})
                with patch('socket.getaddrinfo',side_effect=lambda name,*a,**k:lookup('127.0.0.1' if name=='localhost' else name,*a,**k)):
                    with opener.open(req,timeout=10) as response: return json.load(response)
            deadline=time.monotonic()+120; found=[]
            while time.monotonic()<deadline:
                try:
                    hits=request('https://localhost:19200/logs-ping-*/_search',
                        {'size':100,'query':{'match_phrase':{'log':marker}}})['hits']['hits']
                    found=[(ns,name,value) for ns,name,value in expected if any(
                        value in hit['_source'].get('log','') and hit['_source'].get('namespace')==ns for hit in hits)]
                except urllib.error.HTTPError as error:
                    if error.code!=404: raise
                if len(found)==len(expected): break
                time.sleep(3)
            report={'passed':len(found)==len(expected),'expected':len(expected),'observed':len(found),
                    'marker':marker,'missing':[f'{ns}/{name}' for ns,name,value in expected if (ns,name,value) not in found],
                    'scope':'Synthetic per-pod file probes; does not attest native audit publisher completeness'}
            (project.BUILD/'logging-live-report.json').write_text(json.dumps(report,indent=2)+'\n')
            if not report['passed']: raise ValueError('Missing file probes; see build/logging-live-report.json')
            print(f'PASS: all {len(expected)} live Ping pod file probes indexed with namespace metadata.')
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


if __name__=='__main__': main()
