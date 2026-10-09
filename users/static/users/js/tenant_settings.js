const settingsConfig = document.getElementById("settings-config").dataset;
document.addEventListener("DOMContentLoaded", function () {
    const tablist = document.querySelector("[data-settings-tabs]");
    if (tablist) {
        const tabs = Array.from(tablist.querySelectorAll("[role='tab']"));
        const checkoutForm = document.getElementById("checkout-settings-form");
        const checkoutTabInput = document.querySelector("#checkout-settings-form [name='settings_tab']");

        const tabController = UIComponents.tabs(tablist, { onSelect: function (tab) {
            if (checkoutTabInput && (tab.dataset.tab === "checkout" || tab.dataset.tab === "hours")) {
                checkoutTabInput.value = tab.dataset.tab;
            }
            const url = new URL(window.location.href);
            url.searchParams.set("tab", tab.dataset.tab);
            history.replaceState(null, "", url);
        }});
        function selectSettingsTab(tab) { tabController.select(tab); }

        // Native validation cannot focus controls in a hidden panel. Reveal the
        // first invalid control before asking the browser to show its message.
        checkoutForm.noValidate = true;
        checkoutForm.addEventListener("submit", function (event) {
            const invalid = checkoutForm.querySelector("input:invalid, select:invalid, textarea:invalid");
            if (!invalid) return;
            event.preventDefault();
            const panel = invalid.closest("[role='tabpanel']");
            const tab = tabs.find(item => item.getAttribute("aria-controls") === panel.id);
            selectSettingsTab(tab, true);
            invalid.reportValidity();
        });

        checkoutForm.addEventListener("click", function (event) {
            const link = event.target.closest("[data-settings-error]");
            if (!link) return;
            event.preventDefault();
            selectSettingsTab(tabs.find(tab => tab.dataset.tab === link.dataset.settingsError), true);
            const group = document.getElementById(link.hash.slice(1));
            const control = group.querySelector("input, select, textarea") || group;
            control.focus();
        });


    }

    const generateBtn = document.getElementById("generate-token-button");
    const copyBtn = document.getElementById("copy-token-button");
    const tokenDisplay = document.getElementById("jwt-token-display");
    const tokenContainer = document.getElementById("token-container");
    const timerText = document.getElementById("expire-timer");
    if (!generateBtn) return;
    let tokenInterval = null;

    function clearToken() {
        clearInterval(tokenInterval);
        tokenInterval = null;
        tokenDisplay.textContent = "";
        timerText.textContent = "";
        tokenContainer.hidden = true;
        copyBtn.hidden = true;
        generateBtn.disabled = false;
    }

    generateBtn.addEventListener("click", function () {
        if (generateBtn.disabled) return;
        generateBtn.disabled = true;
        fetch(settingsConfig.tokenUrl, {
            method: "POST",
            headers: {
                "X-CSRFToken": settingsConfig.csrfToken,
                "Content-Type": "application/json"
            }
        })
        .then(response => {
            if (!response.ok) throw new Error("Request failed");
            return response.json();
        })
        .then(data => {
            if (!data.token) throw new Error("Token not received");
            tokenDisplay.textContent = data.token;
            tokenContainer.hidden = false;
            copyBtn.hidden = false;
            notify("Token Generated");

            let countdown = 15;
            timerText.textContent = countdown;
            clearInterval(tokenInterval);
            tokenInterval = setInterval(() => {
                countdown--;
                timerText.textContent = countdown;
                if (countdown <= 0) {
                    clearToken();
                }
            }, 1000);
        })
        .catch(err => {
            clearToken();
            notify("Token Not Generated", "error");
            console.error(err);
        });
    });

    copyBtn.addEventListener("click", async function () {
        const token = tokenDisplay.textContent;
        try {
            await navigator.clipboard.writeText(token);
            notify("Token Copied");
        } catch (_error) {
            notify("Token Not Copied", "error");
        }
    });

    document.querySelectorAll("[data-toggle-visibility]").forEach(btn => {
        btn.addEventListener("click", () => {
            const target = document.getElementById(btn.dataset.toggleVisibility);
            if (target) target.type = target.type === "password" ? "text" : "password";
        });
    });
});
