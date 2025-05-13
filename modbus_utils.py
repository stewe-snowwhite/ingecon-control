from pymodbus.client.sync import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException

def check_modbus_connection(ip_address, port=502, timeout=2):
    try:
        client = ModbusTcpClient(ip_address, port=port, timeout=timeout)
        connection = client.connect()
        client.close()
        return connection
    except ModbusIOException:
        return False
    except Exception as e:
        print(f"Помилка при з'єднанні з {ip_address}: {e}")
        return False
