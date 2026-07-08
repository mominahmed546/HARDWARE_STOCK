document.addEventListener("DOMContentLoaded", function () {
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
