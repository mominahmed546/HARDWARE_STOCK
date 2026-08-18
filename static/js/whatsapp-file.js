(function () {
    function bindWhatsAppFileForm(form) {
        form.addEventListener("submit", async function (event) {
            const fileUrl = form.getAttribute("data-file-url");
            const fileName = form.getAttribute("data-file-name") || "file";
            const mime = form.getAttribute("data-file-type") || "application/octet-stream";
            if (!fileUrl || !navigator.share || !navigator.canShare) {
                return;
            }
            event.preventDefault();
            try {
                const response = await fetch(fileUrl, { credentials: "same-origin" });
                if (!response.ok) {
                    throw new Error("file");
                }
                const file = new File([await response.blob()], fileName, { type: mime });
                if (navigator.canShare({ files: [file] })) {
                    await navigator.share({ files: [file], title: fileName });
                    return;
                }
            } catch (error) {
                if (error && error.name === "AbortError") {
                    return;
                }
            }
            form.submit();
        });
    }

    document.querySelectorAll("form[data-whatsapp-file]").forEach(bindWhatsAppFileForm);
})();
