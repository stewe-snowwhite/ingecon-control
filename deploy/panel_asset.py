"""Prepare/apply/rollback only panel.js; never restarts services or sends controls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

from deploy.discovery_release import ROOT, git, private_json, verify_protected
from operation_locks import resource_lock


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_asset(path, data):
    mode=path.stat().st_mode & 0o777
    fd,temporary=tempfile.mkstemp(prefix='.panel-release-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def service_pids():
    return subprocess.run(['systemctl','show','ingecon-web','ingecon-telegram','ingecon-monitor','ingecon-discovery',
        '--property=Id,MainPID,ActiveState'],check=True,capture_output=True,text=True,timeout=10).stdout


def validate_http(expected):
    from deploy.check_portal import http_check
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for base in ('http://127.0.0.1:5000','http://127.0.0.1:80'):
        with opener.open(base+'/static/panel.js',timeout=10) as response:
            if response.status!=200 or digest(response.read())!=expected:
                raise ValueError('Served panel.js hash mismatch')
        http_check(base)  # Only login/API authorization reads and a CSRF-rejected login POST.


def prepare(release, expected):
    if not re.fullmatch('[0-9a-f]{40}',expected): raise ValueError('Invalid commit SHA')
    if git('branch','--show-current')!='main' or git('rev-parse','HEAD')!=expected:
        raise ValueError('Expected main checkout required')
    if git('ls-remote','origin','refs/heads/main').split()[0]!=expected:
        raise ValueError('Publish the exact commit to origin/main first')
    # main can already contain the published fix while the live asset is kept old.
    if git('status','--porcelain') not in ('','M static/panel.js'):
        raise ValueError('Unexpected working changes; preserve them and stop')
    verify_protected(release)
    directory=Path(release['directory'])
    previous=Path(release['previous_source'])/'static/panel.js'
    target=ROOT/'static/panel.js'
    if target.is_symlink() or previous.is_symlink() or target.read_bytes()!=previous.read_bytes():
        raise ValueError('Active asset differs from the pre-change snapshot')
    candidate=subprocess.run(['git','show',expected+':static/panel.js'],cwd=ROOT,
        check=True,capture_output=True,timeout=10).stdout
    candidate_path=directory/'panel-candidate.js'
    fd=os.open(candidate_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream: stream.write(candidate)
    metadata=dict(version=1,expected_sha=expected,old_hash=digest(previous.read_bytes()),
        new_hash=digest(candidate),candidate=str(candidate_path),previous=str(previous),
        protected=release['protected'],pids=service_pids())
    path=directory/'panel-asset.json'
    private_json(path,metadata)
    return path


def activate(metadata, *, rollback=False):
    if git('branch','--show-current')!='main' or git('rev-parse','HEAD')!=metadata['expected_sha']:
        raise ValueError('Checkout changed; do not overwrite another release')
    if git('status','--porcelain') not in ('','M static/panel.js'):
        raise ValueError('Unexpected working changes; do not overwrite them')
    verify_protected(metadata)
    if not rollback and git('ls-remote','origin','refs/heads/main').split()[0]!=metadata['expected_sha']:
        raise ValueError('Exact commit must be published before activation')
    target=ROOT/'static/panel.js'
    before=metadata['new_hash'] if rollback else metadata['old_hash']
    after=metadata['old_hash'] if rollback else metadata['new_hash']
    source=Path(metadata['previous'] if rollback else metadata['candidate'])
    if target.is_symlink() or source.is_symlink() or digest(source.read_bytes())!=after:
        raise ValueError('Source integrity guard failed')
    current=digest(target.read_bytes())
    if current not in (before,after): raise ValueError('Foreign asset changes; stop without overwriting')
    pids=service_pids()
    try:
        if current!=after: replace_asset(target,source.read_bytes())
        validate_http(after)
        if service_pids()!=pids: raise ValueError('Service state changed during asset activation')
        verify_protected(metadata)
    except Exception:
        # Restore just this file, and only if no other actor changed it.
        if not rollback and digest(target.read_bytes())==after:
            previous=Path(metadata['previous'])
            if digest(previous.read_bytes())!=before: raise ValueError('Rollback source integrity guard failed')
            replace_asset(target,previous.read_bytes())
            validate_http(before)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','apply','rollback'))
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--expected-sha')
    args=parser.parse_args()
    data=json.loads(args.manifest.read_text())
    with resource_lock('panel-asset-release'):
        if args.action=='prepare':
            if not args.expected_sha: parser.error('prepare requires --expected-sha')
            print(prepare(data,args.expected_sha))
        else:
            activate(data,rollback=args.action=='rollback')
            print('Panel asset rollback verified.' if args.action=='rollback' else 'Panel asset update verified; service PIDs unchanged.')


if __name__=='__main__':
    try: main()
    except Exception:
        raise SystemExit('Panel asset operation failed; integrity guards retained. No inverter commands were sent.') from None
