"""Single-file deployment guards and rollback, with HTTP/systemctl/Git mocked."""
from pathlib import Path
import pytest

from deploy import panel_asset as release


@pytest.fixture
def asset(tmp_path,monkeypatch):
    (tmp_path/'static').mkdir()
    target=tmp_path/'static/panel.js'
    target.write_bytes(b'old asset\n')
    target.chmod(0o644)
    previous=tmp_path/'previous.js'; previous.write_bytes(target.read_bytes())
    candidate=tmp_path/'candidate.js'; candidate.write_bytes(b'fixed asset\n')
    metadata=dict(expected_sha='a'*40,old_hash=release.digest(previous.read_bytes()),
        new_hash=release.digest(candidate.read_bytes()),candidate=str(candidate),previous=str(previous),protected={})
    monkeypatch.setattr(release,'ROOT',tmp_path)
    monkeypatch.setattr(release,'git',lambda *args: {
        ('branch','--show-current'):'main',('rev-parse','HEAD'):'a'*40,
        ('status','--porcelain'):'',('ls-remote','origin','refs/heads/main'):'a'*40+'\trefs/heads/main'}[args])
    monkeypatch.setattr(release,'verify_protected',lambda data: None)
    monkeypatch.setattr(release,'service_pids',lambda: 'unchanged pids')
    calls=[]
    monkeypatch.setattr(release,'validate_http',lambda expected: calls.append(expected))
    return target,metadata,calls


def test_apply_and_offline_rollback_only_replace_js(asset):
    target,metadata,calls=asset
    release.activate(metadata)
    assert target.read_bytes()==b'fixed asset\n' and target.stat().st_mode & 0o777==0o644
    release.activate(metadata,rollback=True)
    assert target.read_bytes()==b'old asset\n'
    assert calls==[metadata['new_hash'],metadata['old_hash']]


def test_failed_http_automatically_restores_previous_asset(asset,monkeypatch):
    target,metadata,_=asset
    def validate(expected):
        if expected==metadata['new_hash']: raise ValueError('synthetic HTTP failure')
    monkeypatch.setattr(release,'validate_http',validate)
    with pytest.raises(ValueError,match='synthetic'): release.activate(metadata)
    assert target.read_bytes()==b'old asset\n'


def test_foreign_asset_changes_are_never_overwritten(asset):
    target,metadata,_=asset
    target.write_bytes(b'foreign change')
    with pytest.raises(ValueError,match='Foreign'): release.activate(metadata)
    assert target.read_bytes()==b'foreign change'


def test_other_checkout_changes_block_activation(asset,monkeypatch):
    target,metadata,_=asset
    old_git=release.git
    monkeypatch.setattr(release,'git',lambda *args: 'M app.py' if args==('status','--porcelain') else old_git(*args))
    with pytest.raises(ValueError,match='Unexpected working'): release.activate(metadata)
    assert target.read_bytes()==b'old asset\n'


def test_tampered_candidate_is_rejected(asset):
    target,metadata,_=asset
    Path(metadata['candidate']).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='integrity'): release.activate(metadata)
    assert target.read_bytes()==b'old asset\n'
