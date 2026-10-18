"""Review/apply only web/bot unit cleanup. Never restarts or deletes instance data."""
import argparse
import os
from pathlib import Path
import shutil
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UNITS = ('ingecon-web.service','ingecon-telegram.service')
WRITABLE = '/opt/ingecon-control/instance/production /opt/ingecon-control/instance/locks'
OVERRIDE = 'ingecon-web.service.d/10-demo-write.conf'
EXPECTED = ['[Service]','ReadWritePaths=/opt/ingecon-control/instance/monitor-demo']


def regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Refusing non-regular configuration file: '+str(path))


def cleanup(project=ROOT, system=Path('/etc/systemd/system'), backups=Path('/root'), *, apply=False):
    sources = {}
    for name in UNITS:
        source = project/'deploy'/'systemd'/name
        regular(source)
        text = source.read_text()
        writable = [line.partition('=')[2] for line in text.splitlines() if line.startswith('ReadWritePaths=')]
        if writable != [WRITABLE] or 'demo' in text.lower():
            raise ValueError('Unexpected unit configuration; review before deployment')
        target = system/name
        regular(target)  # Existing production services only, no new service.
        sources[target] = text
    override = system/OVERRIDE
    present = override.exists() or override.is_symlink()
    if present:
        regular(override)
        lines = [line.strip() for line in override.read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if lines != EXPECTED:
            raise ValueError('Override contains additional settings; refusing automatic removal')
    actions = [*sources]+([override] if present else [])
    if not apply:
        return actions,None
    backup = Path(tempfile.mkdtemp(prefix='ingecon-unit-cleanup-',dir=backups))
    backup.chmod(0o700)
    for path in actions:
        shutil.copy2(path,backup/path.name,follow_symlinks=False)
    for target,text in sources.items():
        fd,temporary = tempfile.mkstemp(prefix='.ingecon-unit-',dir=system)
        try:
            with os.fdopen(fd,'w') as stream:
                stream.write(text)
                stream.flush()
                os.fchmod(stream.fileno(),0o644)
            os.replace(temporary,target)
        finally:
            # Only our own temporary /etc file, never instance/ or user data.
            if os.path.exists(temporary): os.unlink(temporary)
    if present: override.unlink()
    return actions,backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Root only: backup/install two units and remove only the verified override')
    args = parser.parse_args(argv)
    if args.apply and os.geteuid()!=0:
        parser.error('--apply requires root; no password is accepted here')
    try:
        actions,backup = cleanup(apply=args.apply)
    except (OSError,ValueError) as error:
        parser.exit(1,str(error)+'\n')
    for path in actions: print(('Updated/removed: ' if args.apply else 'Planned configuration change: ')+str(path))
    if backup: print('Private configuration backup:',backup)
    print('Production writable paths:',WRITABLE)
    print('No services restarted, no runtime files removed, no database or network configuration changed.')
    if args.apply: print('Next: systemctl daemon-reload. Restart web/bot only after separate confirmation.')


if __name__=='__main__': main()
