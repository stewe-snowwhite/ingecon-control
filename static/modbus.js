'use strict';
(() => {
  const inverter = document.getElementById('register-inverter');
  const ip = document.getElementById('register-ip');
  if (inverter && ip) inverter.addEventListener('change', () => {
    ip.value = inverter.selectedOptions[0]?.dataset.ip || '';
  });
})();
