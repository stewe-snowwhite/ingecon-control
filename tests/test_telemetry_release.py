"""No production services, SQLite or network: release guards use synthetic data."""
from pathlib import Path
import sqlite3

import pytest

from deploy import telemetry_release as release

ROOT = Path(__file__).resolve().parents[1]


def row(**changes):
    result = dict(id=1, model='ingecon_sun_100tl',state='Online',stale=False,
                  checks={'modbus':{'ok':True}},active_power_raw=6298,active_power_w=62980,
                  operating_state_code=2,operating_state_label='Підключений до мережі',
                  power_limit_percent=100,limit_reason_code=1)
    return dict(result,**changes)


def test_release_checks_real_fields_and_own_availability():
    release.validate_rows([row()], [1])
    release.validate_rows([row(operating_state_code=7,operating_state_label='Код стану 7',active_power_raw=65534,active_power_w=-20,limit_reason_code=0)], [1])


@pytest.mark.parametrize('changes', [dict(model='unknown'),dict(stale=True),dict(state='Unknown'),
    dict(active_power_w=6298),dict(active_power_raw=None),dict(operating_state_code=None),
    dict(limit_reason_code=None),dict(power_limit_percent=None),dict(operating_state_label='Online'),
    dict(checks={'modbus':{'ok':False}})])
def test_readiness_refuses_missing_stale_or_wrong_scale(changes):
    with pytest.raises(ValueError): release.validate_rows([row(**changes)], [1])


def test_identity_is_bound_to_backup_ip_port_and_unit_id(tmp_path,monkeypatch):
    saved=tmp_path/'saved.sqlite'; live=tmp_path/'live.sqlite'
    for path in (saved,live):
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE inverter (id INTEGER,ip_address TEXT)')
            connection.execute("INSERT INTO inverter VALUES (1,'192.0.2.1')")
            connection.execute('CREATE TABLE monitor_config (inverter_id INTEGER,port INTEGER,device_id INTEGER)')
            connection.execute('INSERT INTO monitor_config VALUES (1,502,1)')
    monkeypatch.setattr(release,'DATABASE',live)
    release.verify_targets({'database_backup':str(saved)},[1])
    with sqlite3.connect(live) as connection:
        connection.execute('UPDATE monitor_config SET device_id=7')
    with pytest.raises(ValueError): release.verify_targets({'database_backup':str(saved)},[1])


def test_activation_has_guarded_rollback_for_both_processes_without_db_restore():
    script=(ROOT/'deploy/activate_telemetry.sh').read_text()
    assert 'trap rollback ERR INT TERM' in script
    assert script.index('-m telemetry_schema') < script.index('systemctl start ingecon-monitor ingecon-web\nready=0')
    assert 'ingecon-web.service.d/91-telemetry-rollback.conf' in script
    assert 'ingecon-monitor.service.d/91-telemetry-rollback.conf' in script
    assert 'old' not in script.split('-m telemetry_schema')[1].split('systemctl start')[0]
    for forbidden in ('git reset','--force','systemctl restart nginx','systemctl stop ingecon-telegram','write_register'):
        assert forbidden not in script
