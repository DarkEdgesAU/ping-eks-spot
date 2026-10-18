#!/usr/bin/env python3
"""Write non-secret lifecycle/backend configuration from applied foundation outputs."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    outputs = json.loads(subprocess.check_output(['terraform', '-chdir=foundation', 'output', '-json'], cwd=ROOT))
    values = {key: item['value'] for key, item in outputs.items()}
    config = json.loads((ROOT/'lifecycle.example.json').read_text())
    for key in ('state_bucket', 'backup_bucket', 'kms_key_arn'):
        config[key] = values[key]
    target = ROOT/'lifecycle.json'
    if target.exists():
        raise ValueError('lifecycle.json already exists; review it rather than overwriting')
    target.write_text(json.dumps(config, indent=2)+'\n')
    for directory, key in [('foundation', 'foundation/terraform.tfstate'), ('terraform', config['state_key'])]:
        settings = {'bucket': values['state_bucket'], 'key': key, 'region': config['region'],
                    'kms_key_id': values['kms_key_arn'], 'encrypt': True, 'use_lockfile': True}
        (ROOT/directory/'backend.hcl').write_text(''.join(name+' = '+json.dumps(value)+'\n' for name, value in settings.items()))
    (ROOT/'foundation/backend.tf').write_text('terraform {\n  backend "s3" {}\n}\n')
    print('Wrote ignored lifecycle/backend settings. Migrate both states before running workflows.')


if __name__ == '__main__': main()
