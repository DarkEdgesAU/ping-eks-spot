#!/usr/bin/env python3
"""Prove actual Fluent Bit -> TLS/authenticated OpenSearch shipping in local Docker."""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import yaml
import observability as obs


def run(keep=False):
    obs.prepare_secrets()
    c=obs.config(); folder=obs.PRIVATE
    logs=folder/'sample/instance/log'; logs.mkdir(parents=True,exist_ok=True)
    output=folder/'collector.conf'
    output.write_text((obs.SOURCE/'sidecar.conf').read_text())
    node_folder=folder/'sample-node/containers'; node_folder.mkdir(parents=True,exist_ok=True)
    node_config=next(x['data']['sidecar.conf'] for x in obs.manifests() if x['metadata']['name']=='node-log-collector' and x['kind']=='ConfigMap')
    (folder/'node.conf').write_text(node_config)
    node_file=node_folder/'probe_logging_container-001.log'
    node_file.touch()
    password=(folder/'shipper-password').read_text()
    dashboard_password=(folder/'dashboards-password').read_text()
    def bind(path, target, read_only=True):
        return {'type':'bind','source':str(path.resolve()),'target':target,'read_only':read_only}
    compose={'name':'ping-logs-poc','services':{
        'config':{'image':c['opensearch_image'],'entrypoint':['sh','/poc/prepare-config.sh'],
            'volumes':[bind(obs.SOURCE,'/poc'),bind(folder,'/auth'), 'config:/work']},
        'opensearch':{'image':c['opensearch_image'],
            'depends_on':{'config':{'condition':'service_completed_successfully'}},
            'environment':{'DISABLE_INSTALL_DEMO_CONFIG':'true','OPENSEARCH_JAVA_OPTS':'-Xms512m -Xmx512m'},
            'ports':['127.0.0.1:19200:9200'],
            'volumes':['config:/usr/share/opensearch/config',bind(folder,'/usr/share/opensearch/config/tls'), 'data:/usr/share/opensearch/data'],
            'networks':{'default':{'aliases':['opensearch.logging.svc.cluster.local']}}},
        'dashboards':{'image':c['dashboards_image'], 'depends_on':['opensearch'],
            'environment':{'DASHBOARDS_PASSWORD':dashboard_password},
            'ports':['127.0.0.1:15601:5601'],
            'volumes':[bind(obs.SOURCE/'dashboards.yml','/usr/share/opensearch-dashboards/config/opensearch_dashboards.yml'),bind(folder,'/tls')]},
        'collector':{'image':c['collector_image'],'depends_on':['opensearch'],
            'command':['-c','/fluent-bit/etc/poc/sidecar.conf'],
            'environment':{'LOG_PASSWORD':password,'LOG_PATHS':'/logs/instance/log/*.log','POD_NAMESPACE':'local-poc','POD_NAME':'sample-ping','PRODUCT':'pingfederate-engine','COLOUR':'blue'},
            'volumes':[bind(folder/'sample','/logs'),bind(output,'/fluent-bit/etc/poc/sidecar.conf'),
                bind(obs.SOURCE/'parsers.conf','/fluent-bit/etc/poc/parsers.conf'),bind(folder,'/credentials'),'buffer:/buffers']},
        'node-collector':{'image':c['collector_image'],'depends_on':['opensearch'],
            'command':['-c','/fluent-bit/etc/poc/sidecar.conf'],
            'environment':{'LOG_PASSWORD':password,'NODE_NAME':'local-poc-node'},
            'volumes':[bind(folder/'sample-node','/var/log'),bind(folder/'node.conf','/fluent-bit/etc/poc/sidecar.conf'),
                bind(obs.SOURCE/'parsers.conf','/fluent-bit/etc/poc/parsers.conf'),bind(folder,'/credentials'),'node-buffer:/buffers']}
    },'volumes':{'data':{},'config':{},'buffer':{},'node-buffer':{}},
        'networks':{'default':{'ipam':{'config':[{'subnet':os.environ.get('LOGGING_POC_SUBNET','10.233.0.0/24')}]}}}}
    path=folder/'compose.yaml'; path.write_text(yaml.safe_dump(compose,sort_keys=False))
    command=['docker','compose','-f',str(path)]
    try:
        subprocess.run(command+['up','-d','--force-recreate'],check=True)
    except subprocess.CalledProcessError:
        subprocess.run(command+['down'],check=False)
        raise
    token=base64.b64encode(('admin:'+(folder/'admin-password').read_text()).encode()).decode()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(folder/'ca.crt'))))
    def request(path, data=None, method='GET'):
        req=urllib.request.Request('https://localhost:19200'+path,
            data=None if data is None else json.dumps(data).encode(),method=method,
            headers={'Authorization':'Basic '+token,'Content-Type':'application/json'})
        with opener.open(req,timeout=8) as response:
            return json.load(response)
    try:
        deadline=time.monotonic()+240
        while True:
            try:
                request('/_cluster/health'); break
            except (urllib.error.URLError,OSError):
                if time.monotonic()>deadline: raise
                time.sleep(3)
        try:
            opener.open('https://localhost:19200/',timeout=8)
            raise RuntimeError('OpenSearch unexpectedly permits unauthenticated access')
        except urllib.error.HTTPError as error:
            if error.code != 401: raise
        probe='poc-'+uuid.uuid4().hex
        expected=[probe+'-server',probe+'-audit',probe+'-restart',probe+'-container']
        for name,marker in [('server.log',expected[0]),('audit.log',expected[1])]:
            with (logs/name).open('a') as stream:
                stream.write(json.dumps({'poc_marker':marker,'event':'synthetic log-shipping probe'})+'\n')
        with node_file.open('a') as stream:
            stream.write(dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')+' stdout F '+expected[3]+'\n')
        def observed():
            try:
                hits=request('/logs-*/_search',{'size':100,'query':{'match_phrase':{'log':probe}}},'POST')['hits']['hits']
                return {marker for marker in expected if any(marker in hit['_source'].get('log','') for hit in hits)}
            except urllib.error.HTTPError as e:
                if e.code==404: return set()
                raise
        deadline=time.monotonic()+90
        while not set(expected[:2]).issubset(observed()):
            if time.monotonic()>deadline: raise RuntimeError('Server/audit file markers not indexed')
            time.sleep(2)
        subprocess.run(command+['restart','collector'],check=True)
        with (logs/'audit.log').open('a') as stream: stream.write(expected[2]+'\n')
        deadline=time.monotonic()+90
        while set(expected)!=observed():
            if time.monotonic()>deadline: raise RuntimeError('Collector did not resume shipping after restart')
            time.sleep(2)
        # Confirm Dashboards serves an authentication page over verified HTTPS.
        deadline=time.monotonic()+180
        while True:
            try:
                with opener.open('https://localhost:15601/',timeout=8) as response:
                    if response.status==200: break
            except (urllib.error.URLError,OSError):
                if time.monotonic()>deadline: raise
            time.sleep(3)
        for name in ('logs-ping','logs-containers'):
            req=urllib.request.Request('https://localhost:15601/api/saved_objects/index-pattern/'+name+'?overwrite=true',
                data=json.dumps({'attributes':{'title':name+'-*','timeFieldName':'@timestamp'}}).encode(),method='POST',
                headers={'Authorization':'Basic '+token,'osd-xsrf':'true','Content-Type':'application/json'})
            with opener.open(req,timeout=10) as response: response.read()
        report={'passed':True,'markers':expected,'indexed':sorted(observed()),
                'tls_verified':True,'collector_restart_verified':True,'dashboards_https':True,
                'anonymous_access_denied':True,'dashboards_data_views_created':True,
                'container_cri_verified':True,
                'scope':'Local synthetic product file logs and simulated node CRI logs; native audit coverage requires live validation'}
        build=obs.ROOT/'build'; build.mkdir(exist_ok=True)
        (build/'logging-smoke.json').write_text(json.dumps(report,indent=2)+'\n')
        print('PASS: server/audit file logs indexed; collector resumed after restart; Dashboards HTTPS responds.')
        print('Evidence:',build/'logging-smoke.json')
    except Exception:
        subprocess.run(command+['logs','--tail','60','opensearch','dashboards','collector','node-collector'],check=False)
        raise
    finally:
        if not keep: subprocess.run(command+['down'],check=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep',action='store_true')
    run(parser.parse_args().keep)
