"""Deny device probes and, by default, command clients; monitoring is separate."""


class DeviceTransportBlocked(RuntimeError):
    pass


def disable_device_transports(*, allow_control=False, allow_register_reads=False):
    """Only an explicitly configured bot may retain its command client."""
    import modbus_utils
    import monitoring
    import pymodbus.client

    def blocked(*args, **kwargs):
        raise DeviceTransportBlocked('Device probes and Modbus operations are disabled in this process')

    if allow_control is not True and allow_register_reads is not True:
        modbus_utils.ModbusTcpClient = blocked
        pymodbus.client.ModbusTcpClient = blocked
    for name in ('icmp', 'tcp', 'modbus', 'poll'):
        setattr(monitoring.NetworkProbes, name, blocked)
