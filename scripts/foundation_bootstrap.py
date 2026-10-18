#!/usr/bin/env python3
"""Prepare persistent foundation inputs/imports; apply only an explicitly reviewed plan."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import boto3

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claims', type=Path, required=True, help='Non-secret output of Inspect lifecycle identity workflow')
    parser.add_argument('--policy', type=Path, default=ROOT/'foundation/deployment-policy.example.json')
    args = parser.parse_args()
    claims = json.loads(args.claims.read_text())
    for key, expected in [('repository_id', '1415576809'), ('repository_owner_id', '299816438'),
                          ('ref', 'refs/heads/main'), ('environment', 'eks-lifecycle'), ('aud', 'sts.amazonaws.com')]:
        if str(claims.get(key)) != expected:
            raise ValueError('Unexpected identity claim: '+key)
    if not claims.get('sub'):
        raise ValueError('Missing verified OIDC subject')
    session = boto3.Session(region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '447061058131':
        raise ValueError('Wrong AWS account')
    iam = session.client('iam')
    providers = [p['Arn'] for p in iam.list_open_id_connect_providers()['OpenIDConnectProviderList']
                 if p['Arn'].endswith('/token.actions.githubusercontent.com')]
    if len(providers) > 1:
        raise ValueError('Ambiguous GitHub OIDC provider')
    values = {'github_oidc_subject': claims['sub'], 'deployment_policy_json': args.policy.read_text(),
              'github_oidc_provider_arn': providers[0] if providers else None}
    (ROOT/'foundation/terraform.tfvars.json').write_text(json.dumps(values, indent=2)+'\n')
    # Declarative imports avoid shell quoting and imperative state removal.
    repos = [repo for page in session.client('ecr').get_paginator('describe_repositories').paginate()
             for repo in page['repositories']]
    imports = []
    for product in ('pingdirectory', 'pingdirectoryproxy', 'pingfederate', 'pingaccess'):
        name = 'pd-spot-dev/'+product
        if any(repo['repositoryName'] == name for repo in repos):
            imports.append('import {\n  to = aws_ecr_repository.images["'+product+'"]\n  id = "'+name+'"\n}\n')
    (ROOT/'foundation/ecr-imports.tf').write_text('\n'.join(imports))
    run(['terraform', '-chdir=foundation', 'init', '-input=false'])
    (ROOT/'build').mkdir(exist_ok=True)
    run(['terraform', '-chdir=foundation', 'plan', '-input=false', '-out=../build/foundation.tfplan'])
    print('Foundation plan prepared. Review it; no infrastructure was applied.')


if __name__ == '__main__':
    try: main()
    except Exception: sys.exit('Foundation preparation failed. Check login, claims and policy; no apply was attempted.')
