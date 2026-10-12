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
        logging.error(f"[Modbus] Помилка при з'єднанні з {ip_address}: {e}")
        return False

def modbus_turn_on(ip, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            client.write_register(1000, 6, device_id=1)  # CMD6: Start inverter
            logging.info(f"[Modbus] 🔌 Інвертор {ip} — запущено (CMD6)")
        else:
            logging.warning(f"[Modbus] ❌ Не вдалося підключитися до {ip} для запуску")
        client.close()
    except Exception as e:
        logging.error(f"[Modbus] ❌ Помилка запуску інвертора {ip}: {e}")

def modbus_turn_off(ip, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            client.write_register(1000, 5, device_id=1)  # CMD5: Stop inverter
            logging.info(f"[Modbus] ⛔ Інвертор {ip} — зупинено (CMD5)")
        else:
            logging.warning(f"[Modbus] ❌ Не вдалося підключитися до {ip} для зупинки")
        client.close()
    except Exception as e:
        logging.error(f"[Modbus] ❌ Помилка зупинки інвертора {ip}: {e}")

def modbus_limit_power(ip, value=50, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            value = max(0, min(int(value), 100))

            # ⚙️ Запис у регістр 46
            result = client.write_register(46, value, device_id=1)
            if result.isError():
                logging.warning(f"[Modbus] ⚠️ Не вдалося записати обмеження для {ip}")
            else:
                logging.info(f"[Modbus] ⚙️ Обмеження {ip} встановлено на {value}%")

                # 📤 Зчитування регістра 46
                rr = client.read_holding_registers(46, count=1, device_id=1)
                if rr and not rr.isError() and hasattr(rr, 'registers') and len(rr.registers) > 0:
                    actual = rr.registers[0]
                    logging.info(f"[Modbus] 📤 Фактичне обмеження: {actual}%")
                else:
                    logging.warning(f"[Modbus] ⚠️ Не вдалося зчитати обмеження — пуста або помилкова відповідь")

                # 🧠 Зчитування причини
                rr_reason = client.read_holding_registers(47, count=1, device_id=1)
                if rr_reason and not rr_reason.isError() and hasattr(rr_reason, 'registers') and len(rr_reason.registers) > 0:
                    reason = rr_reason.registers[0]
                    if reason == 9:
                        logging.info(f"[Modbus] ✅ Причина обмеження: Configuration (A) [код 9]")
                    else:
                        logging.warning(f"[Modbus] ⚠️ Інша причина: код {reason}")
                else:
                    logging.warning(f"[Modbus] ⚠️ Не вдалося зчитати причину обмеження")
        else:
            logging.warning(f"[Modbus] ❌ Не вдалося підключитися до {ip}")
        client.close()
    except Exception as e:
        logging.error(f"[Modbus] ❌ Помилка обмеження потужності {ip}: {e}")

def read_command_status(ip, port=502):
    try:
        client = ModbusTcpClient(ip, port=port)
        if client.connect():
            result = client.read_holding_registers(1000, count=2, device_id=1)
            client.close()
            if result and hasattr(result, 'registers') and len(result.registers) >= 2:
                cmd = result.registers[0]
                scaled = result.registers[1]
                percent = round(scaled / 32767 * 100, 1)
                logging.info(f"[Modbus] 📥 {ip} → Поточне обмеження: {percent}% (scaled={scaled})")
                return cmd, scaled
            else:
                logging.warning(f"[Modbus] ⚠️ Неможливо зчитати статус команди — недостатньо регістрів")
    except Exception as e:
        logging.error(f"[Modbus] ❌ Виняток при читанні статусу з {ip}: {e}")
    return None, None
