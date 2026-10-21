"""Read-only Ingeteam AAA0030IMB02_Q §7.1, ABS1004, pp.25–27.

Manufacturer document viewed via public mirror; compatibility with deployed
hardware/firmware remains to be checked. Offsets are zero-based, FC04 only.
Energy and string-register ranges are intentionally omitted.
"""
from dataclasses import dataclass

SOURCE = 'https://de.scribd.com/document/1045182807/AAA0030IMB02-Q'
DOCUMENT = 'Ingeteam AAA0030IMB02_Q, 03/05/2024, §7.1, 3Play 100TL/160TL (ABS1004)'


@dataclass(frozen=True)
class InputRegister:
    address: int
    name: str
    unit: str = ''
    multiplier: float = 1
    signed: bool = False

    def decode(self, raw):
        if type(raw) is not int or not 0 <= raw <= 65535:
            raise ValueError('Некоректне слово Input Register.')
        if raw == 65535 and not self.signed: return None
        value = raw-65536 if self.signed and raw >= 32768 else raw
        return round(value*self.multiplier, 6)


INPUT_REGISTERS = (
    InputRegister(20, 'Стан інвертора'),
    *(InputRegister(21+i, f'Струм AC L{i+1}', 'A', .01) for i in range(3)),
    *(InputRegister(24+i, f'Напруга AC L{i+1}', 'V', .1) for i in range(3)),
    InputRegister(27, 'Частота мережі', 'Hz', .01),
    InputRegister(28, 'Повна потужність', 'VA', 10, True),
    InputRegister(29, 'Активна потужність', 'W', 10, True),
    InputRegister(30, 'Реактивна потужність', 'VAr', 10, True),
    InputRegister(31, 'Косинус φ', '', .001, True),
    InputRegister(32, 'Загальний струм DC', 'A', .01, True),
    InputRegister(33, 'Загальна напруга DC', 'V'),
    InputRegister(34, 'Загальна потужність DC', 'W', 10, True),
    InputRegister(35, 'Опір ізоляції', 'kΩ'),
    InputRegister(52, 'Фактичне обмеження потужності', '%'),
    InputRegister(53, 'Причина обмеження'),
    InputRegister(54, 'Тип завдання реактивної потужності'),
)
INPUT_BLOCKS = ((20, 16), (52, 3))


def operating_state_label(code):
    if code is None:
        return None
    return 'Підключений до мережі' if code == 2 else f'Код стану {code}'


def telemetry(values):
    """Decode exactly the two complete FC04 blocks; keep raw power for audit."""
    if len(values) != 19 or any(type(v) is not int or not 0 <= v <= 65535 for v in values):
        raise ValueError('Expected complete Input Registers 20–35 and 52–54')
    power = values[9] - 65536 if values[9] >= 32768 else values[9]
    return dict(operating_state_code=None if values[0] == 65535 else values[0],
                active_power_raw=values[9], active_power_w=power * 10,
                power_limit_percent=values[16] if 0 <= values[16] <= 100 else None,
                limit_reason_code=None if values[17] == 65535 else values[17])
