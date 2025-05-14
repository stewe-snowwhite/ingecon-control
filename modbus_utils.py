from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
import logging

# Налаштування логування
logging.basicConfig(
    filename='modbus.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def check_modbus_connection(ip_address, port=502, timeout=2):
    try:
        client = ModbusTcpClient(host=ip_address, port=port, timeout=timeout)
        result = client.connect()
        client.close()
        return result
    except ModbusException:
        return False
    except Exception as e:
        logging.error(f"Помилка при з'єднанні з {ip_address}: {e}")
        return False

def modbus_turn_on(ip, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            client.write_registers(1000, [6])
            logging.info(f"[Modbus] 🔌 Інвертор {ip} — запущено (CMD6)")
        client.close()
    except Exception as e:
        logging.error(f"[Modbus] ❌ Помилка запуску інвертора {ip}: {e}")

def modbus_turn_off(ip, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            client.write_registers(1000, [5])
            logging.info(f"[Modbus] ⛔ Інвертор {ip} — зупинено (CMD5)")
        client.close()
    except Exception as e:
        logging.error(f"[Modbus] ❌ Помилка зупинки інвертора {ip}: {e}")

def modbus_limit_power(ip, value=50, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            scaled = int(value / 100 * 32767)
            client.write_registers(1000, [3, scaled])
            logging.info(f"[Modbus] ⚡ Обмеження потужності {ip} до {value}% (scaled={scaled})")
        client.close()
    except Exception as e:
        logging.error(f"[Modbus] ❌ Помилка обмеження потужності {ip}: {e}")
