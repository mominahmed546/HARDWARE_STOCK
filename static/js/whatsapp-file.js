(function () {
    function whatsappDigits(raw) {
        var digits = String(raw || "").replace(/\D/g, "");
        if (digits.indexOf("00") === 0) { digits = digits.slice(2); }
        if (digits.charAt(0) === "0" && digits.length >= 10) {
            digits = "92" + digits.slice(1);
        } else if (digits.length === 10 && digits.charAt(0) === "3") {
            digits = "92" + digits;
        }
        return (digits.length >= 10 && digits.length <= 15) ? digits : "";
    }

    function saveFile(blob, fileName) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = fileName;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    }

    function canShare(file) {
        try {
            return Boolean(navigator.canShare && navigator.canShare({ files: [file] }));
        } catch (e) { return false; }
    }

    function isFileResponse(r) {
        return r.ok && (r.headers.get("content-type") || "").toLowerCase().indexOf("text/html") === -1;
    }

    function makeFile(blob, fileName, mime) {
        try { return new File([blob], fileName, { type: mime || "application/octet-stream" }); }
        catch (e) { return blob; }
    }

    function loadFile(form) {
        if (form._waFile) { return Promise.resolve(form._waFile); }
        if (form._waPromise) { return form._waPromise; }
        var fileUrl  = form.getAttribute("data-file-url");
        var fileName = form.getAttribute("data-file-name") || "file";
        var mime     = form.getAttribute("data-file-type") || "application/octet-stream";
        form._waPromise = fetch(fileUrl, { credentials: "same-origin", cache: "no-store" })
            .then(function (r) { if (!isFileResponse(r)) { throw new Error("fetch"); } return r.blob(); })
            .then(function (blob) {
                if (!blob || blob.size < 10) { throw new Error("empty"); }
                form._waFile = makeFile(blob, fileName, mime);
                return form._waFile;
            })
            .catch(function (e) { form._waPromise = null; throw e; });
        return form._waPromise;
    }

    function showStep(form, step) {
        form.querySelectorAll("[data-step]").forEach(function (el) {
            el.hidden = el.getAttribute("data-step") !== String(step);
        });
    }

    function setErr(errEl, msg) {
        if (!errEl) { return; }
        errEl.hidden = !msg;
        errEl.textContent = msg || "";
    }

    function bindForm(form) {
        var dlBtn   = form.querySelector("[data-wa-download]");
        var openBtn = form.querySelector("[data-wa-open]");
        var errEl   = form.querySelector("[data-wa-error]");

        /* Pre-load the file so it's ready on first click */
        loadFile(form).catch(function () {});

        if (!dlBtn) { return; }

        dlBtn.addEventListener("click", async function () {
            var numInput = form.querySelector("#whatsapp_number");
            var digits   = whatsappDigits(numInput && numInput.value);
            if (!digits) {
                setErr(errEl, "Enter a valid WhatsApp mobile number first.");
                if (numInput) { numInput.focus(); }
                return;
            }
            setErr(errEl, "");
            dlBtn.disabled = true;
            var origLabel = dlBtn.textContent;
            dlBtn.textContent = "Preparing…";

            try {
                var file     = await loadFile(form);
                var blob     = file;
                var fileName = form.getAttribute("data-file-name") || "file";

                /* Try OS share sheet first — works on Android, iOS,
                   and Windows 10/11 with WhatsApp desktop installed.
                   On those platforms the file is delivered directly. */
                if (canShare(file)) {
                    try {
                        await navigator.share({ files: [file], title: fileName });
                        showStep(form, "done");
                        return;
                    } catch (shareErr) {
                        if (shareErr && shareErr.name === "AbortError") {
                            /* User closed the share sheet — do nothing */
                            return;
                        }
                        /* Share failed (e.g. desktop without WhatsApp app) — fall through */
                    }
                }

                /* Fallback: save the file, then open WhatsApp and show attach instructions */
                saveFile(blob, fileName);
                form.setAttribute("data-wa-digits", digits);
                showStep(form, "2");
            } catch (err) {
                form._waFile    = null;
                form._waPromise = null;
                setErr(errEl, "Could not prepare the file. Use the Download button, then attach it in WhatsApp.");
            } finally {
                dlBtn.disabled  = false;
                dlBtn.textContent = origLabel;
            }
        });

        if (openBtn) {
            openBtn.addEventListener("click", function () {
                var digits = form.getAttribute("data-wa-digits") ||
                             whatsappDigits((form.querySelector("#whatsapp_number") || {}).value);
                if (digits) {
                    window.open("https://wa.me/" + digits, "_blank", "noopener,noreferrer");
                }
            });
        }
    }

    document.querySelectorAll("form[data-whatsapp-file]").forEach(bindForm);
})();
