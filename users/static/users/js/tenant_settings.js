const settingsConfig = document.getElementById("settings-config").dataset;
document.addEventListener("DOMContentLoaded", function () {
    const tablist = document.querySelector("[data-settings-tabs]");
    if (tablist) {
        const tabs = Array.from(tablist.querySelectorAll("[role='tab']"));
        const checkoutForm = document.getElementById("checkout-settings-form");
        const checkoutTabInput = document.querySelector("#checkout-settings-form [name='settings_tab']");
        const optionTablist = document.querySelector("[data-order-options-tabs]");
        const optionTabs = optionTablist ? Array.from(optionTablist.querySelectorAll("[role='tab']")) : [];
        const optionTabInput = checkoutForm.querySelector("[name='order_options_tab']");
        const checkoutActions = checkoutForm.querySelector("[data-checkout-actions]");

        function updateSettingsUrl(tab) {
            const url = new URL(window.location.href);
            url.searchParams.set("tab", tab.dataset.tab);
            if (tab.dataset.tab === "checkout" && optionTabInput) {
                url.searchParams.set("subtab", optionTabInput.value);
            } else {
                url.searchParams.delete("subtab");
            }
            history.replaceState(null, "", url);
        }

        const tabController = UIComponents.tabs(tablist, { onSelect: function (tab) {
            if (checkoutTabInput && (tab.dataset.tab === "checkout" || tab.dataset.tab === "hours")) {
                checkoutTabInput.value = tab.dataset.tab;
            }
            if (checkoutActions) checkoutActions.hidden = !["checkout", "hours"].includes(tab.dataset.tab);
            updateSettingsUrl(tab);
        }});
        const optionController = optionTablist ? UIComponents.tabs(optionTablist, { onSelect: function (tab) {
            optionTabInput.value = tab.dataset.orderOptionsTab;
            updateSettingsUrl(tabs.find(item => item.dataset.tab === "checkout"));
        }}) : null;

        function revealControl(control) {
            const settingsPanel = control.closest("#settings-panel-checkout, #settings-panel-hours");
            if (settingsPanel) {
                tabController.select(tabs.find(tab => tab.getAttribute("aria-controls") === settingsPanel.id));
            }
            const optionPanel = control.closest("[data-order-options-panel]");
            if (optionPanel && optionController) {
                optionController.select(optionTabs.find(tab => tab.getAttribute("aria-controls") === optionPanel.id));
            }
        }

        let validationAttempted = false;
        function updateOptionErrors() {
            if (!validationAttempted) return;
            optionTabs.forEach(tab => {
                const panel = document.getElementById(tab.getAttribute("aria-controls"));
                const hasError = Boolean(panel.querySelector("input:invalid, select:invalid, textarea:invalid, .errorlist"));
                const badge = tab.querySelector(".order-options-error");
                if (hasError && !badge) {
                    const marker = document.createElement("span");
                    marker.className = "order-options-error";
                    marker.textContent = "Errors";
                    tab.appendChild(marker);
                } else if (!hasError && badge) {
                    badge.remove();
                }
            });
        }
        checkoutForm.addEventListener("input", updateOptionErrors);

        // Native validation cannot focus controls in a hidden panel. Reveal the
        // first invalid control before asking the browser to show its message.
        checkoutForm.noValidate = true;
        checkoutForm.addEventListener("submit", function (event) {
            validationAttempted = true;
            updateOptionErrors();
            const invalid = checkoutForm.querySelector("input:invalid, select:invalid, textarea:invalid");
            if (!invalid) return;
            event.preventDefault();
            revealControl(invalid);
            invalid.reportValidity();
        });

        checkoutForm.addEventListener("click", function (event) {
            const link = event.target.closest("[data-settings-error]");
            if (!link) return;
            event.preventDefault();
            const group = document.getElementById(link.hash.slice(1));
            if (!group) return;
            revealControl(group);
            const control = group.querySelector("input, select, textarea") || group;
            control.focus();
        });

        const modeCheckboxes = Array.from(checkoutForm.querySelectorAll("input[name='modes']"));
        function updateModeNotices() {
            checkoutForm.querySelectorAll("[data-order-mode-notice]").forEach(notice => {
                const checkbox = modeCheckboxes.find(input => input.value === notice.dataset.orderModeNotice);
                notice.hidden = Boolean(checkbox && checkbox.checked);
            });
        }
        modeCheckboxes.forEach(input => input.addEventListener("change", updateModeNotices));
        updateModeNotices();
        checkoutForm.addEventListener("click", function (event) {
            const enableButton = event.target.closest("[data-enable-order-mode]");
            if (enableButton) {
                const checkbox = modeCheckboxes.find(input => input.value === enableButton.dataset.enableOrderMode);
                if (checkbox) {
                    checkbox.checked = true;
                    checkbox.dispatchEvent(new Event("change", { bubbles: true }));
                    enableButton.closest("[data-order-options-panel]").querySelector("input, select, textarea")?.focus();
                }
            }
            const optionLink = event.target.closest("[data-order-options-select]");
            if (optionLink && optionController) {
                event.preventDefault();
                const tab = optionTabs.find(item => item.dataset.orderOptionsTab === optionLink.dataset.orderOptionsSelect);
                optionController.select(tab);
                tab.focus();
            }
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
