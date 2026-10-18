"""Deployment cleanup only touches a synthetic /etc tree, never real services."""
from pathlib import Path

import pytest

from deploy.remove_demo_override import cleanup, EXPECTED, OVERRIDE, UNITS, WRITABLE

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tree(tmp_path):
    system = tmp_path/'system'
    backups = tmp_path/'backups'
    system.mkdir()
    backups.mkdir()
    for unit in UNITS:
        (system/unit).write_text('original '+unit)
    override = system/OVERRIDE
    override.parent.mkdir()
    override.write_text('\n'.join(EXPECTED)+'\n')
    return system,backups,override


def test_preview_has_no_side_effects(tree):
    system,backups,override = tree
    actions,backup = cleanup(ROOT,system,backups)
    assert actions==[system/name for name in UNITS]+[override]
    assert backup is None and not list(backups.iterdir())
    assert (system/UNITS[0]).read_text()=='original '+UNITS[0]
    assert override.exists()


def test_apply_backups_override_and_preserves_production_paths(tree):
    system,backups,override = tree
    sentinel = system/'untouched.conf'
    sentinel.write_text('untouched')
    _,backup = cleanup(ROOT,system,backups,apply=True)
    assert not override.exists() and backup.stat().st_mode & 0o777==0o700
    assert (backup/override.name).read_text()=='\n'.join(EXPECTED)+'\n'
    for unit in UNITS:
        assert (backup/unit).read_text()=='original '+unit
        text = (system/unit).read_text()
        assert 'ReadWritePaths='+WRITABLE in text and 'demo' not in text.lower()
    assert sentinel.read_text()=='untouched'


def test_unknown_override_is_not_deleted_or_backed_up_as_success(tree):
    system,backups,override = tree
    override.write_text('[Service]\nReadWritePaths=/important\n')
    with pytest.raises(ValueError,match='additional settings'):
        cleanup(ROOT,system,backups,apply=True)
    assert not list(backups.iterdir()) and '/important' in override.read_text()
    assert (system/UNITS[0]).read_text()=='original '+UNITS[0]


def test_symlink_override_is_never_followed(tree):
    system,backups,override = tree
    other = system/'other.conf'
    other.write_text('do not delete')
    override.unlink()
    override.symlink_to(other)
    with pytest.raises(ValueError,match='non-regular'):
        cleanup(ROOT,system,backups,apply=True)
    assert other.read_text()=='do not delete'


def test_cleanup_repeated_after_removal_is_safe(tree):
    system,backups,_ = tree
    cleanup(ROOT,system,backups,apply=True)
    _,backup = cleanup(ROOT,system,backups,apply=True)
    assert len(list(backup.iterdir()))==2
