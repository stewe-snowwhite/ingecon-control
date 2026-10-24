"""Owner-authorized 3Play test commands. No import-time I/O or arbitrary writes.

AAA0030IMB03_K describes the command block; firmware compatibility is NOT
proven by these tests. Legacy/raw write paths retain their separate interlock.
"""
import hashlib
import ipaddress
import json
import os
import tempfile
from pathlib import Path
import stat

from operation_locks import OperationBusy, endpoint_key, resource_lock

PROFILE = 'ingecon_3play_test_v1'
BUSY_MESSAGE = 'Інша Modbus-операція вже виконується; запис не виконувався.'
AUTHORIZATION = dict(enabled=True, profile=PROFILE, scope='all_registered_100tl',
                     acknowledge_unverified_firmware=True)


def policy_path():
    return Path(os.environ.get('INGECON_INVERTER_CONTROLS_FILE',
        str(Path(__file__).parent/'instance/production/inverter-controls.json')))


def load_policy():
    try:
        fd = os.open(policy_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('Дозвіл керування має бути приватним файлом користувача служби до 4 KiB.')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('Повторні поля дозволу керування.')
                result[key] = value
            return result
        data = json.loads(stream.read(4097), object_pairs_hook=unique)
    if not isinstance(data, dict): raise ValueError('Дозвіл керування має бути JSON-об’єктом.')
    expected = dict(AUTHORIZATION, enabled=data.get('enabled'))
    if data != expected or type(data.get('enabled')) is not bool or data.get('acknowledge_unverified_firmware') is not True:
        raise ValueError('Некоректний дозвіл керування 3Play; довільна карта запису заборонена.')
    return data


def enabled():
    policy = load_policy()
    return bool(policy and policy['enabled'])


def fingerprint():
    return hashlib.sha256(json.dumps(load_policy(), sort_keys=True).encode()).hexdigest()


def set_authorization(active):
    if type(active) is not bool: raise ValueError('Потрібен boolean.')
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with resource_lock('inverter-controls-authorization'):
        load_policy()  # Refuse to overwrite an invalid, insecure or foreign file.
        fd, temporary = tempfile.mkstemp(prefix='.inverter-controls-', dir=path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(dict(AUTHORIZATION, enabled=active), stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Offline owner authorization; never contacts an inverter.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--enable-all-100tl', action='store_true')
    group.add_argument('--disable', action='store_true')
    parser.add_argument('--accept-unverified-firmware', action='store_true')
    args = parser.parse_args(argv)
    if args.enable_all_100tl and not args.accept_unverified_firmware:
        parser.error('Explicit --accept-unverified-firmware is required for this test profile.')
    set_authorization(args.enable_all_100tl)
    print('Authorization enabled for all registered 100TL.' if args.enable_all_100tl else 'Authorization disabled.')


def plan(inverter, transport, action, value=100):
    if inverter.model != 'ingecon_sun_100tl':
        raise ValueError('Керування дозволене лише зареєстрованій моделі INGECON SUN 100TL.')
    address = ipaddress.IPv4Address(inverter.ip_address)
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
        raise ValueError('Недозволена адреса інвертора.')
    if action not in ('on', 'off', 'limit', 'limit_max') or type(value) is not int or not 1 <= value <= 100:
        raise ValueError('Некоректна команда або відсоток потужності (1–100).')
    port, unit = transport['port'], transport['device_id']
    if type(port) is not int or not 1 <= port <= 65535 or type(unit) is not int or not 1 <= unit <= 247:
        raise ValueError('Некоректний порт або Unit ID зареєстрованого інвертора.')
    percent = 100 if action == 'limit_max' else value
    return dict(profile=PROFILE, model=inverter.model, ip=str(address), port=port, device_id=unit,
        action=action, function=6 if action in ('on','off') else 16, address=1000,
        values=[6 if action == 'on' else 5] if action in ('on','off') else [3, percent*32767//100],
        count=1 if action in ('on','off') else 2, percent=percent, firmware_verified=False)


def execute(inverter, transport, action, value=100, *, allow_write=False):
    import modbus_utils
    result = modbus_utils.CommandResult
    if allow_write is not True or not enabled():
        return result(False, 'Керування 3Play вимкнено; Modbus-запис не виконувався.')
    command = plan(inverter, transport, action, value)
    client = None
    try:
        with resource_lock(endpoint_key(command['ip'], command['port'])):
            try:
                if not enabled(): return result(False, 'Дозвіл відкликано; запис не виконувався.')
                client = modbus_utils.ModbusTcpClient(host=command['ip'], port=command['port'], timeout=2, retries=0)
                if not client.connect(): return result(False, 'Немає підключення; запис не виконувався.')
                if command['function'] == 6:
                    reply = client.write_register(1000, command['values'][0], device_id=command['device_id'])
                    echo = getattr(reply, 'registers', None) == command['values']
                else:
                    # One atomic protocol request: never trigger a command before its data.
                    reply = client.write_registers(1000, command['values'], device_id=command['device_id'])
                    echo = getattr(reply, 'count', None) == 2
                if reply is None or reply.isError() or getattr(reply, 'address', None) != 1000 or not echo:
                    return result(False, 'Прийняття команди не підтверджено; перевірте стан перед повторенням.')
                return result(True, 'Пристрій підтвердив прийняття команди; фізичний стан інвертора не підтверджено. Перевірте наступний цикл моніторингу.')
            finally:
                if client is not None: client.close()
    except OperationBusy:
        return result(False, BUSY_MESSAGE)
    except Exception:
        return result(False, 'Результат невідомий; автоматичного повторення немає. Перевірте стан інвертора.')


if __name__ == '__main__':
    main()
