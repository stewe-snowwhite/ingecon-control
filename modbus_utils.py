from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

def check_modbus_connection(ip_address, port=502, timeout=2):
    try:
        client = ModbusTcpClient(host=ip_address, port=port, timeout=timeout)
        result = client.connect()
        client.close()
        return result
    except ModbusException:
        return False
    except Exception as e:
        print(f"Помилка при з'єднанні з {ip_address}: {e}")
        return False
