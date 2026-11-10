"""HTTP transport policy shared by outside-service clients."""

import urllib.request


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None
