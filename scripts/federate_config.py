#!/usr/bin/env python3
"""Run PF configuration Terraform against an explicitly selected inactive colour."""
import argparse
import json
import os
import ssl
import urllib.request
import base64
import contextlib
import subprocess
import time
import federate as pf
import project


@contextlib.contextmanager
def candidate_forward(colour):
    port=19999 if colour=='blue' else 29999
    project.BUILD.mkdir(exist_ok=True)
    log=project.BUILD/f'pf-{colour}-port-forward.log'
    with log.open('w') as stream:
        process=subprocess.Popen(['kubectl','-n',pf.namespace(colour),'port-forward','--address','127.0.0.1',
            f'service/pf-{colour}-pingfederate-admin',f'{port}:9999'],stdout=stream,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise ValueError('Candidate port-forward failed; stop any existing forward on its port')
                if 'Forwarding from 127.0.0.1:' in log.read_text():
                    yield f'https://localhost:{port}'
                    return
                time.sleep(.2)
            raise ValueError('Candidate port-forward did not become ready')
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['plan','apply','replicate'])
    p.add_argument('--target',choices=project.COLOURS,required=True)
    a=p.parse_args(); c=pf.config(); directory=project.config()
    project.check_context(directory)
    if pf.observed_colour()==a.target:
        raise ValueError('Refusing to configure the active PF colour')
    pf.ready(c,a.target)
    password=(pf.SECRET/'federate-admin-password').read_text().strip()
    with candidate_forward(a.target) as url:
        execute(a,c,directory,password,url)


def execute(a,c,directory,password,url):
    if a.command=='replicate':
        token=base64.b64encode(f'Administrator:{password}'.encode()).decode()
        request=urllib.request.Request(url+'/pf-admin-api/v1/cluster/replicate',data=b'',method='POST',
            headers={'Authorization':'Basic '+token,'X-XSRF-Header':'PingFederate','Content-Type':'application/json'})
        with urllib.request.urlopen(request,context=ssl.create_default_context(cafile=str(pf.SECRET/'ca.crt'))) as response:
            print('Candidate configuration replication requested:',response.status)
        return
    env={**os.environ,'PINGFEDERATE_PROVIDER_USERNAME':'Administrator',
         'PINGFEDERATE_PROVIDER_PASSWORD':password,'TF_VAR_colour':a.target,
         'TF_VAR_admin_url':url,'TF_VAR_product_version':c['product_version'],
         'TF_VAR_ca_certificate_file':(pf.SECRET/'ca.crt').as_posix(),
         'TF_VAR_base_dn':directory['base_dn'],'TF_VAR_engine_hostname':c['engine_hostname'],
         'TF_VAR_ldap_password':(pf.SECRET/'federate-ldap-password').read_text().strip()}
    project.run(['terraform','-chdir=terraform-federate','workspace','select','-or-create',a.target],env=env)
    plan=project.BUILD/f'pf-{a.target}.tfplan'
    if a.command=='plan':
        project.run(['terraform','-chdir=terraform-federate','plan',f'-out={plan.as_posix()}'],env=env)
    else:
        if not plan.is_file(): raise ValueError('Create and review the candidate plan first')
        project.run(['terraform','-chdir=terraform-federate','apply',plan.as_posix()],env=env)


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,project.subprocess.CalledProcessError) as e:
        print(f'Error: {e}',file=project.sys.stderr); project.sys.exit(1)
