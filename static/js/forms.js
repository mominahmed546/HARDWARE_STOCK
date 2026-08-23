document.addEventListener("DOMContentLoaded", function () {
    function setupEnterNavigation(form) {
        function isEnter(event) {
            return event.key === "Enter" || event.keyCode === 13;
        }

        function allowsNewline(field, event) {
            if (field.tagName !== "TEXTAREA") {
                return false;
            }
            if (field.id === "notes" || field.hasAttribute("data-enter-newline")) {
                return true;
            }
            return event.shiftKey;
        }

        function focusableFields() {
            return Array.from(form.querySelectorAll(
                'input:not([type="hidden"]):not([type="button"]):not([type="submit"]), select, textarea'
            )).filter(function (field) {
                return !field.disabled && field.tabIndex !== -1 && !field.readOnly;
            });
        }

        function moveToNextField(target) {
            const fields = focusableFields();
            const index = fields.indexOf(target);
            if (index === -1) {
                return false;
            }

            const next = fields[index + 1];
            if (!next) {
                return false;
            }

            next.focus();
            if (
                next.tagName === "INPUT" &&
                typeof next.select === "function" &&
                next.type !== "date" &&
                next.type !== "file"
            ) {
                try {
                    next.select();
                } catch (err) {
                    // Some input types do not support selection.
                }
            }

            if (typeof next.scrollIntoView === "function") {
                next.scrollIntoView({ block: "nearest" });
            }

            return true;
        }

        function handleEnterNavigation(event) {
            if (!isEnter(event) || event.isComposing) {
                return;
            }
            if (event.altKey || event.ctrlKey || event.metaKey) {
                return;
            }

            const target = event.target;
            if (!form.contains(target) || target.tagName === "BUTTON") {
                return;
            }
            if (allowsNewline(target, event)) {
                return;
            }

            event.preventDefault();
            event.stopPropagation();
            moveToNextField(target);
        }

        form.addEventListener("keydown", handleEnterNavigation, true);
        form.addEventListener("keypress", function (event) {
            if (!isEnter(event) || event.isComposing) {
                return;
            }
            const target = event.target;
            if (!form.contains(target) || target.tagName === "BUTTON") {
                return;
            }
            if (allowsNewline(target, event)) {
                return;
            }
            event.preventDefault();
        }, true);
    }

    document.querySelectorAll("form[data-enter-nav]").forEach(setupEnterNavigation);

    document.querySelectorAll("form[data-validate]").forEach(function (form) {
        form.addEventListener("submit", function (event) {
            if (!form.checkValidity()) {
                event.preventDefault();
                form.reportValidity();
            }
        });

        form.querySelectorAll("input, select, textarea").forEach(function (field) {
            field.addEventListener("invalid", function () {
                field.classList.add("is-invalid");
            });

            field.addEventListener("input", function () {
                if (field.checkValidity()) {
                    field.classList.remove("is-invalid");
                }
            });
        });
    });
});
