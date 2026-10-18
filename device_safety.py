"""Disable device I/O in web and Telegram processes; monitoring is separate."""


class DeviceTransportBlocked(RuntimeError):
    pass


def disable_device_transports():
    """Block device clients/probes while leaving the web/Telegram transport intact."""
    import modbus_utils
    import monitoring
    import pymodbus.client

    def blocked(*args, **kwargs):
        raise DeviceTransportBlocked('Device probes and Modbus operations are disabled in this process')

    modbus_utils.ModbusTcpClient = blocked
    pymodbus.client.ModbusTcpClient = blocked
    for name in ('icmp', 'tcp', 'modbus', 'poll'):
        setattr(monitoring.NetworkProbes, name, blocked)
