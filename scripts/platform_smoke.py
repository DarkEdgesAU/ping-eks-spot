#!/usr/bin/env python3
"""Local-only image/configuration smoke test. Containers stop on exit; volumes remain."""
import argparse
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import time
import urllib.request
import yaml
import platform_services as ps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--central', action='store_true', help='Also bootstrap licensed PingCentral using inherited DevOps credentials')
    args = parser.parse_args()
    ps.prepare()
    c = ps.config(); folder = ps.PRIVATE/'smoke'; folder.mkdir(exist_ok=True)
    cfg = ps.metrics_config()
    cfg['global'] = {'scrape_interval': '2s', 'evaluation_interval': '2s'}
    cfg['scrape_configs'] = [{'job_name': 'ping-pods', 'scheme': 'https',
        'tls_config': {'ca_file': '/tls/ca.crt', 'server_name': 'ping-metrics'},
        'static_configs': [{'targets': ['telegraf:9273'], 'labels': {'namespace': 'local-poc', 'pod': 'local-probe'}}]}]
    (folder/'prometheus.yml').write_text(yaml.safe_dump(cfg))
    rules = ps.alerts()
    for rule in rules['groups'][0]['rules']: rule['for'] = '15s'
    (folder/'alerts.yml').write_text(yaml.safe_dump(rules))
    (folder/'telegraf.conf').write_text((ps.SOURCE/'telegraf.conf').read_text().replace('127.0.0.1:8125', ':8125'))
    tls = folder/'tls'; tls.mkdir(exist_ok=True)
    for dest, source in [('tls.crt', 'metrics.crt'), ('tls.key', 'metrics.key'), ('ca.crt', 'ca.crt')]:
        (tls/dest).write_bytes((ps.PRIVATE/source).read_bytes())
    def mount(path, target, ro=True): return str(path.resolve()).replace('\\', '/')+':'+target+(':ro' if ro else '')
    services = {
        'telegraf': {'image': c['telegraf_image'], 'command': ['--config', '/settings/telegraf.conf'],
            'environment': {'HEALTH_URL': 'https://127.0.0.1:1/unavailable'},
            'ports': ['127.0.0.1:18125:8125/udp'], 'volumes': [mount(folder, '/settings'), mount(tls, '/tls')]},
        'prometheus': {'image': c['prometheus_image'], 'command': ['--config.file=/settings/prometheus.yml', '--storage.tsdb.path=/prometheus'],
            'ports': ['127.0.0.1:19090:9090'], 'volumes': [mount(folder, '/settings'), mount(tls, '/tls')]}}
    volumes = {}
    if args.central:
        if not all(os.environ.get(k) for k in ('PING_IDENTITY_DEVOPS_USER', 'PING_IDENTITY_DEVOPS_KEY')):
            raise ValueError('Both DevOps environment variables are required; values are never written to the compose file')
        profile = folder/'central-profile'; profile.mkdir(exist_ok=True)
        data = ps.ping_workload(c, 'pingcentral')[0]['data']
        paths = ['instance/conf/application.properties', 'hooks/07-apply-server-profile.sh.post']
        for i,path in enumerate(paths):
            dest = profile/path; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text(data[f'profile-{i}'], newline='\n')
        services['pingcentral'] = {'image': c['central_image'],
            'environment': ['PING_IDENTITY_DEVOPS_USER', 'PING_IDENTITY_DEVOPS_KEY', 'PING_IDENTITY_ACCEPT_EULA=YES', 'VERBOSE=false', 'PING_DEBUG=false'],
            'ports': ['127.0.0.1:19022:9022'], 'volumes': [mount(profile, '/opt/in'), mount(ps.PRIVATE, '/run/platform'), 'central-ca-data:/opt/out']}
        volumes['central-ca-data'] = {}
    compose = {'services': services, 'networks': {'default': {'ipam': {'config': [{'subnet': os.environ.get('PLATFORM_POC_SUBNET', '10.234.0.0/24')}]}}}, 'volumes': volumes}
    path = folder/'compose.yaml'; path.write_text(yaml.safe_dump(compose, sort_keys=False))
    command = ['docker', 'compose', '-p', 'ping-platform-poc', '-f', str(path)]
    report = {'metrics_scraped': False, 'statsd_received': False, 'alerts_firing': False, 'central_requested': args.central}
    def get(url):
        with urllib.request.urlopen(url, timeout=5) as response: return json.load(response)
    try:
        subprocess.run(command+['up', '-d', '--force-recreate'], check=True)
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b'platform.smoke:42|g', ('127.0.0.1', 18125)); sock.close()
            try:
                result = get('http://127.0.0.1:19090/api/v1/query?query=platform_smoke_value')
                if result['data']['result']:
                    report['statsd_received'] = result['data']['result'][0]['value'][1] == '42'
                    report['metrics_scraped'] = True
                alerts = get('http://127.0.0.1:19090/api/v1/alerts')['data']['alerts']
                report['alerts_firing'] = any(x['labels']['alertname'] == 'PingHealthFailed' and x['state'] == 'firing' for x in alerts)
                if report['statsd_received'] and report['alerts_firing']: break
            except (OSError, ValueError): pass
            time.sleep(3)
        if args.central:
            deadline = time.monotonic()+180
            while time.monotonic() < deadline:
                try:
                    # Bootstrap creates a native self-signed certificate in the DB.
                    # Pin the certificate on this controlled Docker loopback port
                    # for the health request. Native CA import is a separate UI step.
                    pem = ssl.get_server_certificate(('localhost', 19022), timeout=5)
                    ctx = ssl.create_default_context(cadata=pem)
                    ctx.check_hostname = False
                    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
                    with urllib.request.urlopen('https://localhost:19022/actuator/health', context=ctx, timeout=5) as response:
                        report['central_https_health'] = response.status == 200
                        report['central_project_ca_import_pending'] = True
                        break
                except (OSError, ValueError): time.sleep(3)
            if not report.get('central_https_health'): raise ValueError('PingCentral did not become healthy; inspect Docker logs before retrying')
            subprocess.run(command+['exec', '-T', 'pingcentral', 'test', '-s', '/opt/out/instance/conf/pingcentral.lic'], check=True)
            report['central_license_present'] = True
        if not all(report[k] for k in ('metrics_scraped', 'statsd_received', 'alerts_firing')):
            raise ValueError('Local metrics or alert test failed')
        print('Local metrics ingestion, failure alert and requested product health checks passed.')
    finally:
        ps.project.BUILD.mkdir(exist_ok=True)
        (ps.project.BUILD/'platform-smoke.json').write_text(json.dumps(report, indent=2)+'\n')
        subprocess.run(command+['down'], check=True)


if __name__ == '__main__': main()
