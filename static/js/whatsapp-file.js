(function () {
    /* On desktop, Web Share API shows an OS share popup instead of going directly
       to WhatsApp. We detect mobile so the share sheet only runs on phones/tablets. */
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
        setTimeout(function () {
            URL.revokeObjectURL(objectUrl);
        }, 4000);
    }

    function setStatus(form, message, isError) {
        var status = form.querySelector("[data-whatsapp-status]");
        if (!status) {
            return;
        }
        status.hidden = !message;
        status.textContent = message || "";
        status.classList.toggle("whatsapp-file-status-error", Boolean(isError));
    }

    function canShareFiles(file) {
        if (!isMobile()) {
            return false;
        }
        try {
            return Boolean(navigator.canShare && navigator.canShare({ files: [file] }));
        } catch (error) {
            return false;
        }
    }

    function isFileResponse(response) {
        var type = (response.headers.get("content-type") || "").toLowerCase();
        if (!response.ok) {
            return false;
        }
        return type.indexOf("text/html") === -1;
    }

    function makeFile(blob, fileName, mime) {
        try {
            return new File([blob], fileName, { type: mime || blob.type || "application/octet-stream" });
        } catch (error) {
            return blob;
        }
    }

    function loadFormFile(form) {
        if (form._whatsappFile) {
            return Promise.resolve(form._whatsappFile);
        }
        if (form._whatsappFilePromise) {
            return form._whatsappFilePromise;
        }
        var fileUrl = form.getAttribute("data-file-url");
        var fileName = form.getAttribute("data-file-name") || "file";
        var mime = form.getAttribute("data-file-type") || "application/octet-stream";
        form._whatsappFilePromise = fetch(fileUrl, {
            credentials: "same-origin",
            cache: "no-store",
        }).then(function (response) {
            if (!isFileResponse(response)) {
                throw new Error("file");
            }
            return response.blob();
        }).then(function (blob) {
            if (!blob || blob.size < 20) {
                throw new Error("file");
            }
            form._whatsappFile = makeFile(blob, fileName, mime);
            return form._whatsappFile;
        }).catch(function (error) {
            form._whatsappFilePromise = null;
            throw error;
        });
        return form._whatsappFilePromise;
    }

    function bindWhatsAppFileForm(form) {
        var button = form.querySelector("[data-whatsapp-send]") || form.querySelector('button[type="submit"]');
        loadFormFile(form).catch(function () {});

        async function sendFile(event) {
            event.preventDefault();
            event.stopPropagation();
            if (form._whatsappSending) {
                return;
            }

            var fileName = form.getAttribute("data-file-name") || "file";
            var numberInput = form.querySelector("#whatsapp_number");
            var digits = whatsappDigits(numberInput && numberInput.value);
            if (!digits) {
                setStatus(form, "Enter a valid WhatsApp mobile number.", true);
                if (numberInput) {
                    numberInput.focus();
                }
                return;
            }

            form._whatsappSending = true;
            if (button) {
                button.disabled = true;
            }
            setStatus(form, "Preparing the file…");

            try {
                var file = await loadFormFile(form);
                var blob = file instanceof Blob ? file : form._whatsappFile;

                if (canShareFiles(file)) {
                    /* Mobile with share API: let the OS sheet handle it. */
                    try {
                        setStatus(form, "Choose WhatsApp to send the file. The customer can open it without internet.");
                        await navigator.share({ files: [file], title: fileName });
                        setStatus(form, "File sent. The customer can open it offline from WhatsApp.");
                        return;
                    } catch (shareError) {
                        if (shareError && shareError.name === "AbortError") {
                            setStatus(form, "Sharing was cancelled. The file is saved on this device if you want to attach it.");
                            return;
                        }
                        /* If share failed for other reason, fall through to manual path. */
                    }
                }

                /* Desktop and mobile fallback:
                   1. Save the file to the device.
                   2. Open WhatsApp — the user attaches the file with the paperclip. */
                saveFile(blob, fileName);
                if (isMobile()) {
                    setStatus(form, "File saved. Opening WhatsApp — attach the saved file with the paperclip.");
                } else {
                    setStatus(form, "File downloaded. Opening WhatsApp Web — attach the downloaded file with the paperclip icon.");
                }
                window.setTimeout(function () {
                    window.open("https://wa.me/" + digits, "_blank", "noopener,noreferrer");
                }, 600);
            } catch (error) {
                form._whatsappFile = null;
                form._whatsappFilePromise = null;
                setStatus(form, "Could not prepare the file. Use the Download button on this page, then attach that file in WhatsApp.", true);
            } finally {
                form._whatsappSending = false;
                if (button) {
                    button.disabled = false;
                }
            }
        }

        if (button) {
            button.addEventListener("click", sendFile);
        }
        form.addEventListener("submit", sendFile);
    }

    document.querySelectorAll("form[data-whatsapp-file]").forEach(bindWhatsAppFileForm);
})();
