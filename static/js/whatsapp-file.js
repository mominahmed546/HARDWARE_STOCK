(function () {
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
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () {
            URL.revokeObjectURL(objectUrl);
        }, 2000);
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

    function bindWhatsAppFileForm(form) {
        var button = form.querySelector('button[type="submit"]');
        form.addEventListener("submit", async function (event) {
            event.preventDefault();
            if (!form.checkValidity()) {
                form.reportValidity();
                return;
            }

            var fileUrl = form.getAttribute("data-file-url");
            var fileName = form.getAttribute("data-file-name") || "file";
            var mime = form.getAttribute("data-file-type") || "application/octet-stream";
            var numberInput = form.querySelector("#whatsapp_number");
            var digits = whatsappDigits(numberInput && numberInput.value);
            if (!fileUrl) {
                setStatus(form, "The file could not be prepared.", true);
                return;
            }
            if (!digits) {
                setStatus(form, "Enter a valid WhatsApp mobile number.", true);
                return;
            }

            if (button) {
                button.disabled = true;
            }
            setStatus(form, "Saving the file on this device…");

            try {
                var response = await fetch(fileUrl, { credentials: "same-origin" });
                if (!response.ok) {
                    throw new Error("file");
                }
                var blob = await response.blob();
                var file = new File([blob], fileName, { type: mime });
                saveFile(blob, fileName);

                if (navigator.canShare && navigator.canShare({ files: [file] })) {
                    setStatus(form, "Choose WhatsApp and send the saved file. The customer can open it without internet.");
                    await navigator.share({ files: [file], title: fileName });
                    setStatus(form, "File sent. The customer can open it offline from WhatsApp.");
                    return;
                }

                setStatus(
                    form,
                    "The file is saved on this device. Attach that downloaded file in WhatsApp (paperclip). Do not send a link."
                );
                window.open("https://wa.me/" + digits, "_blank", "noopener,noreferrer");
            } catch (error) {
                if (error && error.name === "AbortError") {
                    setStatus(form, "The file is saved on this device. Attach it in WhatsApp if it was not sent.");
                    return;
                }
                setStatus(form, "Could not prepare the file. Download it on this page, then attach that file in WhatsApp.", true);
            } finally {
                if (button) {
                    button.disabled = false;
                }
            }
        });
    }

    document.querySelectorAll("form[data-whatsapp-file]").forEach(bindWhatsAppFileForm);
})();
