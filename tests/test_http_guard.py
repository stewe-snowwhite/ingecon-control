"""Offline network inventory and firewall rendering; subprocess/network blocked."""
import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import http_guard as guard
import portal
import portal_hosts
from tests.test_portal import http_portal
from deploy import check_portal

ROOT = Path(__file__).resolve().parents[1]


def nic(name='eth-test', address='10.42.0.2', prefix=24):
    return dict(ifname=name, link_type='ether', operstate='UP', flags=['UP','BROADCAST'],
                addr_info=[dict(family='inet', local=address, prefixlen=prefix, scope='global')])


def route(name='eth-test', subnet='10.42.0.0/24', address='10.42.0.2'):
    return dict(dev=name, dst=subnet, prefsrc=address, scope='link', protocol='dhcp')


def discover(links=None, routes=None, **kwargs):
    return guard.discover(links if links is not None else [nic()],
                          routes if routes is not None else [route()], physical=lambda _: True, **kwargs)


def test_current_dhcp_lan_and_network_move():
    assert discover() == {'lan': [['eth-test','10.42.0.0/24','10.42.0.2']], 'hosts': ['10.42.0.2']}
    moved = discover([nic(address='172.20.5.10')], [route(subnet='172.20.5.0/24', address='172.20.5.10')])
    assert moved['lan'] == [['eth-test','172.20.5.0/24','172.20.5.10']]
    assert '10.42.0.2' not in moved['hosts']


@pytest.mark.parametrize('address,prefix', [('8.8.4.4',24),('100.100.10.2',24),('169.254.8.2',24),
                                         ('10.1.2.3',8),('10.42.0.2',31),('127.0.0.2',24)])
def test_public_cgnat_linklocal_and_ambiguous_prefix_fail_closed(address,prefix):
    subnet = str(ipaddress.ip_network(f'{address}/{prefix}',strict=False))
    assert discover([nic(address=address,prefix=prefix)], [route(subnet=subnet,address=address)])['lan'] == []


@pytest.mark.parametrize('change', [dict(gateway='10.42.0.1'), dict(scope='global'), dict(dev='other'),
                                   dict(type='blackhole'),dict(nhid=2),dict(flags=['linkdown']),
                                   dict(prefsrc='10.42.0.99')])
def test_no_gateway_routed_remote_networks_or_conflicting_route(change):
    current = route()
    current.update(change)
    assert discover(routes=[current])['lan'] == []


def test_no_default_only_route_duplicate_route_or_overlapping_interfaces():
    assert discover(routes=[dict(dev='eth-test',dst='default',gateway='10.42.0.1')])['lan'] == []
    assert discover(routes=[route(),route()])['lan'] == []
    assert discover([nic(),nic('eth-other','10.42.0.3')],
                    [route(),route('eth-other',address='10.42.0.3')])['lan'] == []


@pytest.mark.parametrize('change',[dict(operstate='DOWN'),dict(flags=['UP','POINTOPOINT']),
                                  dict(linkinfo={'info_kind':'veth'}),dict(link_type='none')])
def test_no_inactive_tunnel_bridge_or_virtual_lan(change):
    current = nic()
    current.update(change)
    assert discover([current])['lan'] == []


def test_physical_device_required():
    assert guard.discover([nic()],[route()],physical=lambda _:False)['lan'] == []


def test_tailscale_remains_available_with_lan_disabled_and_vpn_ip_changes():
    tail = dict(ifname='tailscale0',flags=['UP','POINTOPOINT'],addr_info=[
        dict(local='100.90.1.2',scope='global'),dict(local='fd7a:115c:a1e0::123',scope='global')])
    result = discover([nic(),tail],lan_enabled=False)
    assert result == {'lan': [], 'hosts': ['100.90.1.2','fd7a:115c:a1e0::123']}
    tail['addr_info'][0]['local'] = '100.110.1.20'
    assert discover([tail],routes=[])['hosts'][0] == '100.110.1.20'


def test_invalid_interface_cannot_inject_nft_syntax():
    assert discover([nic('bad";drop')])['lan'] == []
    with pytest.raises(ValueError):
        guard.render_rules({'lan': [['bad";drop','10.42.0.0/24','10.42.0.2']]},'# LAN_ELEMENTS')


def test_rules_only_http_have_short_lan_leases_and_destination_pairing():
    template = (ROOT/'deploy/nftables/http-guard.nft').read_text()
    rules = guard.render_rules(discover(),template)
    assert '"eth-test" . 10.42.0.0/24 . 10.42.0.2' in rules
    assert 'timeout 15s' in rules and 'flags interval,timeout' in rules
    assert 'fib daddr type local' in rules and 'fib saddr . iif oif exists' in rules
    assert 'hook output' not in rules and 'hook forward' not in rules
    assert 'flush ruleset' not in rules and 'policy drop' not in rules
    assert all('dport 80' in line for line in rules.splitlines()
               if (' accept' in line or ' drop' in line) and 'policy ' not in line)
    baseline = guard.render_rules({'lan':[]},template)
    assert 'elements =' not in baseline and 'iifname "tailscale0"' in baseline


def test_runtime_state_write_only_on_change(tmp_path,monkeypatch):
    target = tmp_path/'state.json'
    monkeypatch.setattr(guard,'STATE',target)
    guard.write_state(discover())
    before = target.stat().st_mtime_ns
    guard.write_state(discover())
    assert target.stat().st_mtime_ns == before
    guard.write_state({'lan': [], 'hosts': []})
    assert json.loads(target.read_text())['hosts'] == []


def test_host_state_rejects_user_owned_or_writable_files(tmp_path,monkeypatch):
    target = tmp_path/'state.json'
    target.write_text(json.dumps({'hosts':['10.42.0.2']}))
    monkeypatch.setattr(portal_hosts,'STATE',target)
    original = portal_hosts.os.fstat
    def root_info(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode,st_uid=0,st_size=info.st_size)
    monkeypatch.setattr(portal_hosts.os,'fstat',root_info)
    target.chmod(0o644)
    assert '10.42.0.2' in portal_hosts.current_hosts()
    target.chmod(0o666)
    assert '10.42.0.2' not in portal_hosts.current_hosts()
    monkeypatch.setattr(portal_hosts.os,'fstat',lambda fd:SimpleNamespace(st_mode=0o100644,st_uid=1001,st_size=10))
    assert '10.42.0.2' not in portal_hosts.current_hosts()


def test_host_state_fails_closed_missing_malformed_symlink(tmp_path,monkeypatch):
    target = tmp_path/'state.json'
    monkeypatch.setattr(portal_hosts,'STATE',target)
    baseline = portal_hosts.current_hosts()
    target.write_text('{invalid')
    assert portal_hosts.current_hosts() == baseline
    target.unlink()
    target.symlink_to(tmp_path/'other')
    assert portal_hosts.current_hosts() == baseline


def test_host_validation_updates_without_restart(http_portal,monkeypatch):
    allowed = ['monitoring.local','10.42.0.2','100.90.1.2','[fd7a:115c:a1e0::123]']
    monkeypatch.setattr(portal,'current_hosts',lambda:allowed)
    client = http_portal.test_client()
    assert client.get('http://10.42.0.2/login').status_code == 200
    assert client.get('http://100.90.1.2/login').status_code == 200
    assert client.get('http://[fd7a:115c:a1e0::123]/login').status_code == 200
    allowed[:] = ['monitoring.local','172.20.5.10']
    assert client.get('http://10.42.0.2/login').status_code == 400
    assert client.get('http://172.20.5.10/login').status_code == 200
    assert client.get('http://evil.invalid/login').status_code == 400


def test_guard_unit_runs_root_owned_copy_and_stops_nginx_on_failure():
    unit = (ROOT/'deploy/systemd/ingecon-http-guard.service').read_text()
    assert '/usr/bin/python3 -I /usr/local/libexec/ingecon-http-guard.py' in unit
    assert 'Type=notify' in unit and 'WatchdogSec=30' in unit
    assert 'CAP_NET_ADMIN' in unit and 'AF_NETLINK AF_UNIX' in unit
    assert 'ExecStop=' not in '\n'.join(line for line in unit.splitlines() if not line.startswith('#'))
    dropin = (ROOT/'deploy/systemd/nginx.service.d/ingecon.conf').read_text()
    assert 'BindsTo=ingecon-http-guard.service' in dropin


def test_existing_unknown_firewall_table_is_never_replaced(monkeypatch):
    tables = {'nftables':[{'table':{'family':'inet','name':'ingecon_http_portal'}}]}
    table = {'nftables':[{'table':{'comment':'other administrator policy'}}]}
    results = iter([tables,table])
    monkeypatch.setattr(guard,'execute',lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps(next(results))))
    with pytest.raises(RuntimeError,match='not owned'):
        guard.check_ownership()


@pytest.mark.parametrize('broken_inventory',[False,True])
def test_daemon_installs_baseline_before_ready_and_discovery_failure_closes_lan(tmp_path,monkeypatch,broken_inventory):
    class StopCycle(Exception):
        pass
    template = (ROOT/'deploy/nftables/http-guard.nft').read_text()
    monkeypatch.setattr(guard.os,'geteuid',lambda:0)
    monkeypatch.setattr(guard,'TEMPLATE',SimpleNamespace(
        lstat=lambda:SimpleNamespace(st_mode=0o100644,st_uid=0),read_text=lambda:template))
    monkeypatch.setattr(guard,'STATE',tmp_path/'state.json')
    monkeypatch.setattr(guard,'check_ownership',lambda:None)
    if broken_inventory:
        def broken():
            raise OSError('netlink unavailable')
        monkeypatch.setattr(guard,'inventory',broken)
    else:
        monkeypatch.setattr(guard,'inventory',lambda:([nic()],[route()]))
        original = guard.discover
        monkeypatch.setattr(guard,'discover',lambda *args,**kwargs:original(*args,physical=lambda _:True,**kwargs))
    calls, notifications = [], []
    monkeypatch.setattr(guard,'execute',lambda args,**kwargs:calls.append((args,kwargs.get('input',''))))
    monkeypatch.setattr(guard,'notify',lambda message:notifications.append((message,len(calls))))
    monkeypatch.setattr(guard.time,'sleep',lambda _:(_ for _ in ()).throw(StopCycle()))
    with pytest.raises(StopCycle): guard.run()
    assert len(calls) == 3 and '--check' in calls[0][0]
    assert 'elements =' not in calls[1][1]  # Initial protection before inventory.
    assert ('10.42.0.0/24' in calls[2][1]) is not broken_inventory
    assert notifications == [('READY=1\nWATCHDOG=1',3)]
    assert bool(json.loads((tmp_path/'state.json').read_text())['lan']) is not broken_inventory


def test_activation_backups_precede_network_change_and_no_duplicate_polling():
    script = (ROOT/'deploy/activate_portal.sh').read_text()
    assert script.index('nft list ruleset >') < script.index('systemctl restart ingecon-http-guard.service')
    assert script.index('deploy.prepare_portal') < script.index('systemctl start ingecon-web.service')
    assert script.index('Existing manual Telegram poller') < script.index('systemctl start ingecon-telegram.service')
    assert 'kill ' not in script and 'nft flush' not in script and 'apt-get' not in script
    assert 'systemctl restart tailscaled' not in script and 'systemctl restart ssh' not in script


def test_post_activation_http_probe_only_reads_and_rejects_missing_csrf(monkeypatch):
    requests = []
    class Response:
        def __init__(self,status):
            self.status = status
            self.headers = {'Set-Cookie':'session=test; HttpOnly; SameSite=Lax'}
        def __enter__(self): return self
        def __exit__(self,*args): pass
    class Opener:
        def open(self,request,timeout):
            requests.append(request)
            if request.data is not None: return Response(400)
            return Response(401 if request.full_url.endswith('/api/statuses') else 200)
    monkeypatch.setattr(check_portal.urllib.request,'build_opener',lambda *args:Opener())
    check_portal.http_check('http://127.0.0.1:5000')
    assert len(requests) == 3 and all('/control' not in r.full_url for r in requests)
    assert requests[-1].data == b'username=invalid-probe'
    assert all(request.host == '127.0.0.1:5000' for request in requests)
    assert all(not request.has_header('Host') for request in requests)


@pytest.fixture
def health_checks(monkeypatch):
    calls = []
    network = {'addresses':[nic()], 'routes':[route()], 'vpn_active':False}
    monkeypatch.setattr(check_portal,'service_check',lambda name:calls.append(('service',name)))
    monkeypatch.setattr(check_portal,'inventory',lambda:(network['addresses'],network['routes']))
    monkeypatch.setattr(check_portal,'discover',lambda *args:guard.discover(*args,physical=lambda _:True))
    monkeypatch.setattr(check_portal,'http_check',lambda base:calls.append(('http',base)))
    monkeypatch.setattr(check_portal,'telegram_check',lambda:calls.append(('telegram',)))
    def service_state(args,**kwargs):
        assert args == ['systemctl','is-active','tailscaled']
        return SimpleNamespace(returncode=0 if network['vpn_active'] else 3)
    monkeypatch.setattr(check_portal.subprocess,'run',service_state)
    return calls, network


def test_local_http_and_telegram_pass_without_tailscale(health_checks,capsys):
    calls,_ = health_checks
    check_portal.main([])
    assert ('http','http://10.42.0.2') in calls and ('telegram',) in calls
    assert ('service','tailscaled') not in calls
    assert 'Optional Tailscale unavailable' in capsys.readouterr().out


def test_health_checks_follow_dhcp_change_without_dns_or_restart(health_checks):
    calls, network = health_checks
    check_portal.main([])
    assert ('http','http://10.42.0.2') in calls
    calls.clear()
    network['addresses'] = [nic(address='172.20.5.10')]
    network['routes'] = [route(subnet='172.20.5.0/24',address='172.20.5.10')]
    check_portal.main([])
    assert ('http','http://172.20.5.10') in calls
    assert ('http','http://10.42.0.2') not in calls


def test_optional_tailscale_checked_in_addition_to_primary_lan(health_checks):
    calls,network = health_checks
    network['vpn_active'] = True
    network['addresses'].append(dict(ifname='tailscale0',flags=['UP'],addr_info=[
        dict(family='inet',local='100.90.1.2',scope='global'),
        dict(family='inet6',local='fd7a:115c:a1e0::123',scope='global')]))
    check_portal.main([])
    assert ('http','http://10.42.0.2') in calls
    assert ('http','http://100.90.1.2') in calls
    assert ('http','http://[fd7a:115c:a1e0::123]') in calls
    assert ('telegram',) in calls


def test_failed_optional_vpn_does_not_skip_primary_lan_or_telegram(health_checks):
    calls,network = health_checks
    network['vpn_active'] = True  # A broken running VPN must still be reported.
    with pytest.raises(RuntimeError,match='Optional Tailscale'):
        check_portal.main([])
    assert ('http','http://10.42.0.2') in calls and ('telegram',) in calls


@pytest.mark.parametrize('explicit_fallback',[False,True])
def test_unsafe_or_missing_lan_is_reported_without_widening_rules(health_checks,explicit_fallback):
    calls,network = health_checks
    network['addresses'],network['routes'] = [],[]
    if explicit_fallback:
        check_portal.main(['--allow-no-lan'])
    else:
        with pytest.raises(RuntimeError,match='LAN discovery'):
            check_portal.main([])
    assert ('telegram',) in calls
    assert [entry for entry in calls if entry[0]=='http'] == [
        ('http','http://127.0.0.1:5000'),('http','http://127.0.0.1:80')]


def test_telegram_error_is_redacted_and_other_channels_checked(health_checks,monkeypatch,capsys):
    calls,_ = health_checks
    def error():
        raise RuntimeError('SECRET-TOKEN-in-URL')
    monkeypatch.setattr(check_portal,'telegram_check',error)
    with pytest.raises(RuntimeError,match='Telegram') as captured:
        check_portal.main([])
    output = capsys.readouterr().out
    assert 'SECRET-TOKEN' not in output+str(captured.value)
    assert 'Optional Tailscale unavailable' in output
    assert ('http','http://10.42.0.2') in calls


def test_deployment_has_no_tailscale_dependency_for_lan_or_telegram():
    for path in (ROOT/'deploy/systemd').rglob('*.service'):
        lines = path.read_text().splitlines()
        for line in lines:
            if line.startswith(('Requires=','BindsTo=','After=','Wants=')):
                assert 'tailscaled' not in line
    script = (ROOT/'deploy/activate_portal.sh').read_text()
    assert '\nsystemctl is-active --quiet tailscaled.service\n' not in script
    assert 'if [[ "$tailscale_active_before" == active ]]; then systemctl is-active --quiet tailscaled.service; fi' in script


def test_primary_lan_render_refuses_unsafe_network_before_any_rule_change(monkeypatch,tmp_path,capsys):
    monkeypatch.setattr(guard,'inventory',lambda:([],[]))
    template = tmp_path/'rules.nft'
    template.write_text('# LAN_ELEMENTS')
    monkeypatch.setattr(guard.sys,'argv',['http_guard.py','--render','--require-lan','--template',str(template)])
    with pytest.raises(SystemExit) as error:
        guard.main()
    assert error.value.code == 2
    assert 'No safely determined LAN; no rules applied' in capsys.readouterr().err


def test_explicit_no_lan_render_preserves_safe_fallback(monkeypatch,tmp_path,capsys):
    monkeypatch.setattr(guard,'inventory',lambda:([nic()],[route()]))
    monkeypatch.setenv('INGECON_HTTP_LAN','false')
    template = tmp_path/'rules.nft'
    template.write_text('set lan { # LAN_ELEMENTS }')
    monkeypatch.setattr(guard.sys,'argv',['http_guard.py','--render','--template',str(template)])
    guard.main()
    assert capsys.readouterr().out.strip() == 'set lan {  }'
