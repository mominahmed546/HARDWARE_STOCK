(function () {
    /* Phone numbers → international format */
    function toDigits(raw) {
        var d = String(raw || "").replace(/\D/g, "");
        if (d.indexOf("00") === 0) d = d.slice(2);
        if (d.charAt(0) === "0" && d.length >= 10) d = "92" + d.slice(1);
        else if (d.length === 10 && d.charAt(0) === "3") d = "92" + d;
        return (d.length >= 10 && d.length <= 15) ? d : "";
    }

    function makeFile(blob, name, mime) {
        try { return new File([blob], name, { type: mime }); }
        catch (e) { return blob; }
    }

    /* Fetch the file bytes and cache them on the form element */
    function loadFile(form) {
        if (form._waBlob) return Promise.resolve(form._waBlob);
        if (form._waPromise) return form._waPromise;
        var url  = form.getAttribute("data-file-url");
        var name = form.getAttribute("data-file-name") || "file";
        var mime = form.getAttribute("data-file-type") || "application/octet-stream";
        form._waPromise = fetch(url, { credentials: "same-origin", cache: "no-store" })
            .then(function (r) {
                if (!r.ok) throw new Error(r.status);
                var ct = (r.headers.get("content-type") || "").toLowerCase();
                if (ct.indexOf("text/html") !== -1) throw new Error("html");
                return r.blob();
            })
            .then(function (blob) {
                if (!blob || blob.size < 10) throw new Error("empty");
                form._waBlob = makeFile(blob, name, mime);
                return form._waBlob;
            })
            .catch(function (e) { form._waPromise = null; throw e; });
        return form._waPromise;
    }

    function setStatus(btn, msg) {
        var s = btn.closest("form").querySelector("[data-wa-status]");
        if (s) { s.textContent = msg; s.hidden = !msg; }
    }

    function bindForm(form) {
        var btn = form.querySelector("[data-wa-send]");
        if (!btn) return;

        /* Pre-load immediately so first click is instant */
        loadFile(form).catch(function () {});

        btn.addEventListener("click", async function () {
            var numInput = form.querySelector("[data-wa-number]") || form.querySelector("input[type=tel]");
            var digits   = toDigits(numInput && numInput.value);
            if (!digits) {
                alert("Enter a valid WhatsApp mobile number first.");
                if (numInput) numInput.focus();
                return;
            }

            btn.disabled  = true;
            var origText  = btn.textContent;
            btn.textContent = "Preparing…";

            try {
                var file = await loadFile(form);
                var name = form.getAttribute("data-file-name") || "file";

                /* ── Path A: Web Share API (Android, iOS) ─────────────────
                   On these platforms navigator.share({ files }) opens the OS
                   share sheet. Picking WhatsApp delivers the actual file into
                   the chat — recipient opens it offline. */
                if (navigator.canShare && navigator.canShare({ files: [file] })) {
                    try {
                        setStatus(btn, "Choose WhatsApp in the share sheet…");
                        await navigator.share({ files: [file], title: name });
                        setStatus(btn, "✓ File sent.");
                        return;
                    } catch (shareErr) {
                        if (shareErr.name === "AbortError") {
                            setStatus(btn, "");  /* user closed sheet — do nothing */
                            return;
                        }
                        /* share failed for other reason → fall through */
                    }
                }

                /* ── Path B: desktop / unsupported browser ────────────────
                   Save the file locally, then open the wa.me chat so the
                   user can attach it with the paperclip. */
                var blobUrl = URL.createObjectURL(file);
                var a = document.createElement("a");
                a.href = blobUrl; a.download = name;
                document.body.appendChild(a); a.click(); a.remove();
                setTimeout(function () { URL.revokeObjectURL(blobUrl); }, 5000);

                setStatus(btn, "File saved — opening WhatsApp. Attach it with the paperclip.");
                setTimeout(function () {
                    window.open(
                        "https://api.whatsapp.com/send/?phone=" + digits + "&text&type=phone_number&app_absent=0",
                        "_blank", "noopener,noreferrer"
                    );
                }, 500);

            } catch (err) {
                form._waBlob    = null;
                form._waPromise = null;
                setStatus(btn, "Could not load the file. Check your connection.");
            } finally {
                btn.disabled    = false;
                btn.textContent = origText;
            }
        });
    }

    document.querySelectorAll("form[data-whatsapp-file]").forEach(bindForm);
})();
