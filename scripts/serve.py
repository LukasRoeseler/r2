#!/usr/bin/env python3
"""Serve _site/ locally under the same subpath GitHub Pages will use.

http://localhost:8737/  ->  _site/  (replicationresearch.github.io is a
<user-or-org>.github.io repo, so GitHub Pages serves it at the domain root)

Forces correct JavaScript MIME types (the Windows registry can map .js/.mjs
to text/plain, which breaks pdf.js module loading in the browser).
"""

import http.server
import os

PORT = 8737
PREFIX = os.environ.get("BASE_URL", "/").rstrip("/")
SITE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "_site")


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".ftl": "text/plain",
    }

    def __init__(self, *args, **kw):
        super().__init__(*args, directory=SITE, **kw)

    def translate_path(self, path):
        if PREFIX and path.startswith(PREFIX):
            path = path[len(PREFIX):] or "/"
        elif PREFIX:
            # Anything outside the prefix 404s, like on GitHub Pages.
            path = "/__outside_prefix__" + path
        return super().translate_path(path)


if __name__ == "__main__":
    print("Serving %s at http://localhost:%d%s/" % (SITE, PORT, PREFIX))
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
