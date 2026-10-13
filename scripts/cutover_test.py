"""Explicit JMeter LDAP cutover actions. No cloud deployment or automatic promotion."""
import argparse
import csv
import html
import json
import math
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def load_config(path):
    c = json.loads(Path(path).read_text())
    for key in ('password_file', 'truststore', 'truststore_pin_file'):
        c[key] = str((ROOT / c[key]).resolve())
    for key in ('clients', 'duration', 'timeout_ms', 'rate'):
        if not isinstance(c[key], int) or c[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if c['clients'] < 2:
        raise ValueError('Use at least two clients to exercise fresh and persistent connections')
    if c['max_errors'] < 0 or c['max_p95_ms'] <= 0:
        raise ValueError('Invalid pass thresholds')
    for url in [c['proxy_url']] + [r['url'] for r in c['replicas']]:
        u = urlparse(url)
        if u.scheme != 'ldaps' or not u.hostname or u.username or u.path not in ('', '/'):
            raise ValueError('Endpoints must be ldaps://host:port without credentials or a base DN')
        if not u.port:
            raise ValueError('Specify an explicit LDAPS port')
    return c


def prop(node, name, value, kind='stringProp'):
    if kind == 'doubleProp':
        element = ET.SubElement(node, kind)
        ET.SubElement(element, 'name').text = name
        ET.SubElement(element, 'value').text = str(float(value))
        ET.SubElement(element, 'savedValue').text = '0.0'
    else:
        ET.SubElement(node, kind, name=name).text = str(value)


def plan(mode, c):
    root = ET.Element('jmeterTestPlan', version='1.2', properties='5.0', jmeter='5.6.3')
    tree = ET.SubElement(root, 'hashTree')
    test = ET.SubElement(tree, 'TestPlan', guiclass='TestPlanGui', testclass='TestPlan', testname='LDAP cutover', enabled='true')
    prop(test, 'TestPlan.functional_mode', 'false', 'boolProp')
    prop(test, 'TestPlan.serialize_threadgroups', 'true', 'boolProp')
    variables = ET.SubElement(test, 'elementProp', name='TestPlan.user_defined_variables', elementType='Arguments', guiclass='ArgumentsPanel', testclass='Arguments', enabled='true')
    ET.SubElement(variables, 'collectionProp', name='Arguments.arguments')
    testtree = ET.SubElement(tree, 'hashTree')
    group = ET.SubElement(testtree, 'ThreadGroup', guiclass='ThreadGroupGui', testclass='ThreadGroup', testname=mode, enabled='true')
    prop(group, 'ThreadGroup.on_sample_error', 'continue')
    controller = ET.SubElement(group, 'elementProp', name='ThreadGroup.main_controller', elementType='LoopController', guiclass='LoopControlPanel', testclass='LoopController', enabled='true')
    prop(controller, 'LoopController.continue_forever', 'false', 'boolProp')
    prop(controller, 'LoopController.loops', -1 if mode == 'load' else 1)
    prop(group, 'ThreadGroup.num_threads', c['clients'] if mode == 'load' else 1)
    prop(group, 'ThreadGroup.ramp_time', 10 if mode == 'load' else 0)
    prop(group, 'ThreadGroup.scheduler', str(mode == 'load').lower(), 'boolProp')
    prop(group, 'ThreadGroup.duration', c['duration'] if mode == 'load' else '')
    prop(group, 'ThreadGroup.delay', 0)
    gt = ET.SubElement(testtree, 'hashTree')
    sampler = ET.SubElement(gt, 'JSR223Sampler', guiclass='TestBeanGUI', testclass='JSR223Sampler', testname=mode, enabled='true')
    prop(sampler, 'scriptLanguage', 'groovy')
    prop(sampler, 'filename', (ROOT / 'loadtest/ldap.groovy').as_posix())
    prop(sampler, 'cacheKey', 'true')
    prop(sampler, 'parameters', '')
    prop(sampler, 'script', '')
    ET.SubElement(gt, 'hashTree')
    if mode == 'load':
        timer = ET.SubElement(gt, 'ConstantThroughputTimer', guiclass='TestBeanGUI', testclass='ConstantThroughputTimer', testname='Aggregate operation rate', enabled='true')
        prop(timer, 'throughput', c['rate'] * 60, 'doubleProp')
        prop(timer, 'calcMode', 4, 'intProp')
        ET.SubElement(gt, 'hashTree')
    return ET.ElementTree(root)


def read_samples(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def summarize(rows):
    latencies = sorted(float(r['elapsed']) for r in rows)
    return {'requests': len(rows), 'errors': sum(r['success'].lower() != 'true' for r in rows),
            'p95_ms': latencies[max(0, math.ceil(len(latencies) * .95) - 1)] if rows else None}


def report(run, c):
    rows = read_samples(run / 'load.jtl')
    markers = json.loads((run / 'markers.json').read_text()) if (run / 'markers.json').exists() else {}
    start, end = markers.get('promotion-start'), markers.get('promotion-end')
    stages = {'all': summarize(rows)}
    valid_markers = start is not None and end is not None and start < end
    if valid_markers:
        # Include any request spanning either promotion boundary in the cutover bucket.
        before, during, after = [], [], []
        for row in rows:
            ts = float(row['timeStamp'])
            finish = ts + float(row['elapsed'])
            (before if finish < start else after if ts > end else during).append(row)
        stages.update(before=summarize(before), during=summarize(during), after=summarize(after))
    reconciliation = json.loads((run / 'reconciliation.json').read_text()) if (run / 'reconciliation.json').exists() else {}
    completed = (run / 'load-completed.json').exists()
    passed = bool(rows) and completed and valid_markers and all(stages[s]['requests'] > 0 for s in ('before', 'during', 'after'))
    passed = passed and all(s['errors'] <= c['max_errors'] and s['p95_ms'] is not None and s['p95_ms'] <= c['max_p95_ms'] for s in stages.values())
    passed = bool(passed and reconciliation.get('passed') and reconciliation.get('uncertain') == 0)
    result = {'passed': passed, 'load_completed': completed, 'stages': stages, 'markers': markers, 'reconciliation': reconciliation,
              'scope': 'One configured base DN; observed cutover only; not proof of RPO=0'}
    (run / 'summary.json').write_text(json.dumps(result, indent=2))
    table = ''.join(f'<tr><td>{name}</td><td>{s["requests"]}</td><td>{s["errors"]}</td><td>{s["p95_ms"]}</td></tr>' for name, s in stages.items())
    (run / 'summary.html').write_text('<!doctype html><meta charset="utf-8"><title>LDAP cutover</title>'
        '<style>body{font:16px system-ui;margin:3em;max-width:1000px}td,th{padding:.6em;border:1px solid #ccc}table{border-collapse:collapse}pre{white-space:pre-wrap}</style>'
        f'<h1>LDAP cutover: {"PASS" if passed else "FAIL / INCOMPLETE"}</h1>'
        '<table><tr><th>Phase</th><th>Requests</th><th>Errors</th><th>p95 ms</th></tr>' + table + '</table>'
        '<p>Detailed time-series charts: <a href="dashboard/index.html">JMeter dashboard</a>.</p>'
        '<p>Missing markers, missing phase samples, uncertain writes, or incomplete reconciliation fail the report.</p>'
        '<pre>' + html.escape(json.dumps(result, indent=2)) + '</pre>', encoding='utf-8')
    print(json.dumps({'passed': passed, 'stages': stages}, indent=2))
    return passed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['init', 'seed', 'run', 'mark', 'reconcile', 'report', 'cleanup'])
    p.add_argument('--config', default='loadtest/config.json')
    p.add_argument('--run', default='build/cutover')
    p.add_argument('--jmeter', default='jmeter')
    p.add_argument('--event', choices=['promotion-start', 'promotion-end'])
    a = p.parse_args()
    run = Path(a.run).resolve()
    if not run.is_relative_to(ROOT / 'build'):
        raise ValueError('Run directories must be inside ignored build/ to protect test credentials')
    if a.action == 'mark':
        if not a.event: p.error('--event is required')
        markers = json.loads((run / 'markers.json').read_text()) if (run / 'markers.json').exists() else {}
        if a.event in markers: raise ValueError('Marker already exists; use a new run')
        markers[a.event] = int(time.time() * 1000)
        (run / 'markers.json').write_text(json.dumps(markers, indent=2))
        return 0
    if a.action == 'init':
        c = load_config(a.config)
        run.mkdir(parents=True, exist_ok=False)
        ou = 'cutover-' + uuid.uuid4().hex
        (run / 'manifest.json').write_text(json.dumps({'ou': ou, 'test_dn': f'ou={ou},{c["base_dn"]}'}, indent=2))
        (run / 'config.json').write_text(json.dumps(c, indent=2))
        (run / 'user-password').write_text(secrets.token_urlsafe(32))
        (run / 'user-password').chmod(0o600)
        (run / 'writes.jsonl').touch()
        print(f'Prepared {run}; no LDAP requests sent')
        return 0
    c = load_config(run / 'config.json')
    manifest = json.loads((run / 'manifest.json').read_text())
    if not re.fullmatch(r'cutover-[0-9a-f]{32}', manifest['ou']) or manifest['test_dn'] != f'ou={manifest["ou"]},{c["base_dn"]}':
        raise ValueError('Invalid test OU manifest; refusing LDAP actions')
    if a.action == 'report': return 0 if report(run, c) else 1
    if a.action == 'reconcile':
        # Reload endpoints only, allowing separate direct port forwards after the load stops.
        endpoints = load_config(a.config)['replicas']
        expected = {f'{colour}-{i}' for colour in ('blue', 'green') for i in range(3)}
        if {r['name'] for r in endpoints} != expected or len(endpoints) != 6 or len({r['url'] for r in endpoints}) != 6:
            raise ValueError('Supply six distinct direct replica URLs named blue-0..2 and green-0..2')
        c['replicas'] = endpoints
        (run / 'config.json').write_text(json.dumps(c, indent=2))
    mode = 'load' if a.action == 'run' else a.action
    jtl = run / f'{mode}.jtl'
    if jtl.exists(): raise ValueError('Action already ran; use a new run directory (do not overwrite evidence)')
    if mode == 'load' and (not (run / 'seed.jtl').exists() or any(r['success'] != 'true' for r in read_samples(run / 'seed.jtl'))):
        raise ValueError('Seed must succeed before load')
    binary = shutil.which(a.jmeter)
    if not binary: raise ValueError('JMeter is not on PATH; use --jmeter with its executable path')
    binary = str(Path(binary).resolve())
    for key in ('password_file', 'truststore', 'truststore_pin_file'):
        if not Path(c[key]).is_file(): raise ValueError(f'Missing {key}')
    jmx = run / f'{mode}.jmx'
    plan(mode, c).write(jmx, encoding='utf-8', xml_declaration=True)
    cmd = [binary, '-n', '-t', str(jmx), '-l', str(jtl), '-j', str(run / f'{mode}.log'),
           f'-Jcutover.config={(run / "config.json").as_posix()}', f'-Jcutover.run={run.as_posix()}', f'-Jcutover.mode={mode}',
           '-Jjmeter.save.saveservice.output_format=csv', '-Jjmeter.save.saveservice.print_field_names=true',
           '-Jjmeter.save.saveservice.timestamp_format=ms', '-Jjmeter.save.saveservice.response_data=false',
           '-Jjmeter.save.saveservice.samplerData=false', '-Jsampleresult.timestamp.start=true']
    if mode == 'load': cmd += ['-e', '-o', str(run / 'dashboard')]
    subprocess.run(cmd, check=True, cwd=ROOT)
    rows = read_samples(jtl)
    if mode == 'load' and rows:
        span = max(float(r['timeStamp']) + float(r['elapsed']) for r in rows) - min(float(r['timeStamp']) for r in rows)
        if span < c['duration'] * 800:
            raise ValueError('Load ended early; refusing to mark it complete')
        (run / 'load-completed.json').write_text(json.dumps({'finished_at': int(time.time() * 1000), 'span_ms': span}))
    return 0 if rows and all(r['success'].lower() == 'true' for r in rows) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
