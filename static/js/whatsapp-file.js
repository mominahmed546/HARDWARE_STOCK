(function () {
    function isMobile() {
        return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    }

    function whatsappDigits(raw) {
        var digits = String(raw || "").replace(/\D/g, "");
        if (digits.indexOf("00") === 0) {
            digits = digits.slice(2);
        }
        if (digits.charAt(0) === "0" && digits.length >= 10) {
            digits = "92" + digits.slice(1);
        } else if (digits.length === 10 && digits.charAt(0) === "3") {
            digits = "92" + digits;
        }
        if (digits.length >= 10 && digits.length <= 15) {
            return digits;
        }
        return "";
    }

    function saveFile(blob, fileName) {
        var objectUrl = URL.createObjectURL(blob);
        var link = document.createElement("a");
        link.href = objectUrl;
        link.download = fileName;
        link.rel = "noopener";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 4000);
    }

    function canShareFiles(file) {
        if (!isMobile()) { return false; }
        try {
            return Boolean(navigator.canShare && navigator.canShare({ files: [file] }));
        } catch (e) { return false; }
    }

    function isFileResponse(response) {
        var type = (response.headers.get("content-type") || "").toLowerCase();
        return response.ok && type.indexOf("text/html") === -1;
    }

    function makeFile(blob, fileName, mime) {
        try {
            return new File([blob], fileName, { type: mime || blob.type || "application/octet-stream" });
        } catch (e) { return blob; }
    }

    function loadFormFile(form) {
        if (form._whatsappFile) { return Promise.resolve(form._whatsappFile); }
        if (form._whatsappFilePromise) { return form._whatsappFilePromise; }
        var fileUrl  = form.getAttribute("data-file-url");
        var fileName = form.getAttribute("data-file-name") || "file";
        var mime     = form.getAttribute("data-file-type") || "application/octet-stream";
        form._whatsappFilePromise = fetch(fileUrl, { credentials: "same-origin", cache: "no-store" })
            .then(function (r) {
                if (!isFileResponse(r)) { throw new Error("file"); }
                return r.blob();
            })
            .then(function (blob) {
                if (!blob || blob.size < 20) { throw new Error("file"); }
                form._whatsappFile = makeFile(blob, fileName, mime);
                return form._whatsappFile;
            })
            .catch(function (err) {
                form._whatsappFilePromise = null;
                throw err;
            });
        return form._whatsappFilePromise;
    }

    function showStep(form, step) {
        form.querySelectorAll("[data-step]").forEach(function (el) {
            el.hidden = el.getAttribute("data-step") !== String(step);
        });
    }

    function bindWhatsAppFileForm(form) {
        var dlBtn   = form.querySelector("[data-wa-download]");
        var openBtn = form.querySelector("[data-wa-open]");
        var errEl   = form.querySelector("[data-wa-error]");

        function showError(msg) {
            if (errEl) { errEl.hidden = false; errEl.textContent = msg; }
        }
        function clearError() {
            if (errEl) { errEl.hidden = true; errEl.textContent = ""; }
        }

        loadFormFile(form).catch(function () {});

        if (dlBtn) {
            dlBtn.addEventListener("click", async function () {
                var numberInput = form.querySelector("#whatsapp_number");
                var digits = whatsappDigits(numberInput && numberInput.value);
                if (!digits) {
                    showError("Enter a valid WhatsApp mobile number first.");
                    if (numberInput) { numberInput.focus(); }
                    return;
                }
                clearError();
                dlBtn.disabled = true;
                dlBtn.textContent = "Preparing…";

                try {
                    var file = await loadFormFile(form);
                    var blob = file instanceof Blob ? file : form._whatsappFile;
                    var fileName = form.getAttribute("data-file-name") || "file";

                    if (canShareFiles(file)) {
                        /* Mobile: OS share sheet → pick WhatsApp */
                        try {
                            await navigator.share({ files: [file], title: fileName });
                            showStep(form, "done");
                            return;
                        } catch (shareErr) {
                            if (shareErr && shareErr.name === "AbortError") {
                                showStep(form, "1");
                                return;
                            }
                        }
                    }

                    /* Desktop / mobile fallback: save file, show step 2 */
                    saveFile(blob, fileName);
                    form.setAttribute("data-wa-digits", digits);
                    showStep(form, "2");
                } catch (err) {
                    form._whatsappFile = null;
                    form._whatsappFilePromise = null;
                    showError("Could not prepare the file. Try the Download button above.");
                } finally {
                    dlBtn.disabled = false;
                    dlBtn.textContent = dlBtn.getAttribute("data-label") || "Step 1 — Save file & prepare";
                }
            });
        }

        if (openBtn) {
            openBtn.addEventListener("click", function () {
                var digits = form.getAttribute("data-wa-digits") || "";
                if (!digits) {
                    var numberInput = form.querySelector("#whatsapp_number");
                    digits = whatsappDigits(numberInput && numberInput.value);
                }
                if (digits) {
                    window.open("https://wa.me/" + digits, "_blank", "noopener,noreferrer");
                }
            });
        }
    }

    document.querySelectorAll("form[data-whatsapp-file]").forEach(bindWhatsAppFileForm);
})();
