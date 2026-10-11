'use strict';
const el = (id) => document.getElementById(id);
let number = '';
el('copy-number').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(number);
    el('phone-status').textContent = 'Copied. Open Messages and text your first question.';
  } catch (_) { el('phone-status').textContent = 'Select and copy the number above, then open Messages.'; }
});
(async () => {
  try {
    const response = await fetch('/v1/public-config');
    if (!response.ok) throw new Error('Configuration unavailable');
    number = (await response.json()).service_number;
    el('service-number').textContent = number;
    el('open-messages').href = `sms:${number}`;
    el('open-messages').hidden = false;
    el('copy-number').disabled = false;
  } catch (_) {
    el('service-number').textContent = 'Number unavailable';
    el('phone-status').textContent = 'Refresh the page to try again.';
  }
})();
