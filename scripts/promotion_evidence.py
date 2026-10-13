"""Generate promotion metadata; operator attestations remain explicit."""
import argparse
import datetime as dt
import getpass
import json
from pathlib import Path
import subprocess
import sys
import project

FLAGS = ('replication_verified_all_base_dns', 'writes_reconciled',
         'rollback_compatible', 'direct_writers_accounted_for')


def generate(target, operator, notes, attest=False):
    c = project.config()
    project.check_context(c)
    project.ready_pods(f'identity-{target}', 'pingdirectory', c['image_tag'])
    if not operator.strip() or not notes.strip():
        raise ValueError('Operator and verification notes are required')
    return dict(cluster_name=c['cluster_name'], target_colour=target,
                image_tag=c['image_tag'], base_dn=c['base_dn'],
                verified_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                operator=operator, evidence_notes=notes,
                **{flag: attest for flag in FLAGS})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True, choices=project.COLOURS)
    p.add_argument('--output', help='Defaults to build/<target>-evidence.json')
    p.add_argument('--operator', default=getpass.getuser())
    p.add_argument('--notes', required=True, help='Record actual verification checks and report paths')
    p.add_argument('--attest-verified', action='store_true',
                   help='Explicitly attest all four verification checks have just been completed')
    p.add_argument('--overwrite', action='store_true', help='Replace a previous evidence file')
    a = p.parse_args()
    output = Path(a.output or f'build/{a.target}-evidence.json').resolve()
    if not output.is_relative_to(project.BUILD):
        raise ValueError('Write evidence inside ignored build/')
    if output.exists() and not a.overwrite:
        raise ValueError('Evidence already exists; use --overwrite after repeating verification')
    evidence = generate(a.target, a.operator, a.notes, a.attest_verified)
    if a.attest_verified:
        project.validate_evidence(evidence, project.config(), a.target)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(f'Wrote {output}')
    print('Operator attestation saved; promote within five minutes.' if a.attest_verified
          else 'Draft only: verification flags remain false. Readiness and placement do not verify replication or writes.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
