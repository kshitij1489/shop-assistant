/* City selection is verified again by the server when contact details are saved. */
(() => {
    const form = document.getElementById('contact-settings-form');
    if (!form) return;
    const city = form.elements.city;
    const list = document.getElementById('city-suggestions');
    const status = document.getElementById('city-status');
    let timer, controller, revision = 0, active = -1, results = [];

    function close() {
        list.hidden = true;
        city.setAttribute('aria-expanded', 'false');
        city.removeAttribute('aria-activedescendant');
        active = -1;
    }

    function cancel() {
        clearTimeout(timer);
        revision++;
        if (controller) controller.abort();
        close();
    }

    function invalidate() {
        form.elements.city_place_id.value = '';
        form.elements.state.value = '';
        form.elements.country.value = '';
        city.setCustomValidity('Select a city from the suggestions.');
    }

    async function lookup(params) {
        controller = new AbortController();
        const url = new URL(form.dataset.citiesUrl, window.location.origin);
        Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
        const response = await fetch(url, {signal: controller.signal, headers: {'Accept': 'application/json'}});
        if (response.redirected) throw new Error('Your session expired. Reload this page and sign in.');
        let data;
        try { data = await response.json(); }
        catch (_) { throw new Error('Location lookup is unavailable. Please try again.'); }
        if (!response.ok) throw new Error(data.error || 'Location lookup is unavailable. Please try again.');
        return data;
    }

    async function select(index) {
        const result = results[index];
        if (!result) return;
        cancel();
        const current = revision;
        invalidate();
        status.textContent = 'Validating city…';
        try {
            const selected = await lookup({place_id: result.place_id});
            if (current !== revision) return;
            ['city', 'state', 'country', 'city_place_id'].forEach(key => { form.elements[key].value = selected[key]; });
            city.setCustomValidity('');
            status.textContent = 'City selected. Enter your street address and postal code.';
            form.elements.street_address_1.focus();
        } catch (error) {
            if (current === revision && error.name !== 'AbortError') status.textContent = error.message;
        }
    }

    async function search() {
        const query = city.value.trim();
        if (query.length < 2) {
            status.textContent = 'Type at least two characters of a city, optionally followed by its country.';
            return;
        }
        const current = revision;
        status.textContent = 'Searching cities…';
        try {
            const data = await lookup({q: query});
            if (current !== revision) return;
            results = data.results;
            list.replaceChildren();
            results.forEach((result, index) => {
                const option = document.createElement('li');
                option.id = `city-option-${index}`;
                option.setAttribute('role', 'option');
                option.setAttribute('aria-selected', 'false');
                option.textContent = result.label;
                option.addEventListener('pointerdown', event => event.preventDefault());
                option.addEventListener('mousedown', event => event.preventDefault());
                option.addEventListener('click', () => select(index));
                list.appendChild(option);
            });
            list.hidden = results.length === 0;
            city.setAttribute('aria-expanded', String(results.length > 0));
            status.textContent = results.length ? 'Select a city. Use the arrow keys and Enter, or click a result.' : 'No cities found. Try a city and country name.';
        } catch (error) {
            if (current === revision && error.name !== 'AbortError') status.textContent = error.message;
        }
    }

    city.addEventListener('input', () => {
        cancel();
        invalidate();
        timer = setTimeout(search, 300);
    });
    city.addEventListener('focus', () => {
        cancel();
        if (!form.elements.city_place_id.value) search();
    });
    city.addEventListener('blur', cancel);
    city.addEventListener('keydown', event => {
        if (event.key === 'Escape') { cancel(); return; }
        if (list.hidden) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            active = (active + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
            Array.from(list.children).forEach((option, index) => option.setAttribute('aria-selected', String(index === active)));
            city.setAttribute('aria-activedescendant', list.children[active].id);
            list.children[active].scrollIntoView({block: 'nearest'});
        } else if (event.key === 'Enter') {
            event.preventDefault();
            if (active >= 0) select(active);
        }
    });
    form.addEventListener('submit', event => {
        if (!form.elements.city_place_id.value) {
            event.preventDefault();
            city.setCustomValidity('Select a city from the suggestions.');
            city.reportValidity();
        }
    });
})();
