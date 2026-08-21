"""Gzip text responses (HTML/CSS/JS/JSON) so mobile/slow connections transfer
fewer bytes per page.

Deliberately hand-rolled instead of pulling in Flask-Compress: that package
imports the `brotli` C-extension unconditionally at module load time, so if
it isn't installed (or fails to build) on the host, the whole app would fail
to start. gzip is in the Python standard library and already understood by
every browser, which covers most of the same win with no new dependency and
no way to break app startup.
"""

import gzip

from flask import request

_COMPRESSIBLE_MIMETYPES = {
    "text/html",
    "text/css",
    "text/plain",
    "text/xml",
    "text/javascript",
    "application/javascript",
    "application/json",
    "application/xml",
    "image/svg+xml",
}

_MIN_SIZE_BYTES = 500


def init_compression(app):
    @app.after_request
    def _gzip_response(response):
        vary = response.headers.get("Vary")
        if not vary:
            response.headers["Vary"] = "Accept-Encoding"
        elif "accept-encoding" not in vary.lower():
            response.headers["Vary"] = f"{vary}, Accept-Encoding"

        if response.mimetype not in _COMPRESSIBLE_MIMETYPES:
            return response
        if response.direct_passthrough or response.is_streamed:
            return response
        if "Content-Encoding" in response.headers:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if "gzip" not in request.accept_encodings:
            return response

        data = response.get_data()
        if len(data) < _MIN_SIZE_BYTES:
            return response

        response.set_data(gzip.compress(data, compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = response.content_length
        return response

    return app
