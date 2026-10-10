document.querySelectorAll('[data-rule-set]').forEach(function (group) {
    const total = group.querySelector('[name$="-TOTAL_FORMS"]');
    const add = group.querySelector('[data-add-rule]');
    function toggleDeleted(checkbox) {
        checkbox.closest('[data-rule-row]').querySelectorAll('input, select, textarea').forEach(function (input) {
            if (input !== checkbox) input.disabled = checkbox.checked;
        });
    }
    group.querySelectorAll('[name$="-DELETE"]:checked').forEach(toggleDeleted);
    group.addEventListener('change', function (event) {
        if (event.target.name?.endsWith('-DELETE')) toggleDeleted(event.target);
    });
    add.addEventListener('click', function () {
        const index = Number(total.value);
        if (index >= 100) return;
        const template = group.querySelector('[data-rule-template]');
        const rows = group.querySelector('[data-rule-rows]');
        rows.insertAdjacentHTML('beforeend', template.innerHTML.replaceAll('__prefix__', String(index)));
        total.value = index + 1;
        add.disabled = index + 1 >= 100;
        updateMoneyLabels();
        rows.lastElementChild.querySelector('input, select').focus();
    });
});

function updateMoneyLabels() {
    const form = document.getElementById('ordering-settings-form');
    if (!form) return;
    const currency = form.querySelector('[name="currency"]').value;
    form.querySelectorAll('label').forEach(function (label) {
        if (!/\((?:₹ )?[A-Z]{3}\)$/.test(label.textContent.replace(/:$/, ''))) return;
        label.textContent = label.textContent.replace(/\((?:₹ )?[A-Z]{3}\)/, '(' + (currency === 'INR' ? '₹ INR' : currency) + ')');
        const input = document.getElementById(label.htmlFor);
        if (input) {
            const scale = currency === 'JPY' ? 1 : 100;
            input.step = String(1 / scale);
            input.max = String(9999999999 / scale);
            input.min = input.name.startsWith('max_') ? String(1 / scale) : '0';
        }
    });
}
document.querySelector('#ordering-settings-form [name="currency"]')?.addEventListener('change', updateMoneyLabels);
