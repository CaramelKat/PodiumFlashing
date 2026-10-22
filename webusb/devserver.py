#!/usr/bin/env python3
"""Local dev server that mirrors the GitHub Pages layout.

Serves this folder at /, the bundled images from ../marvell_flash_tool at /images/ (only the
ones the page needs, never 83_IMAGE) and ../firmware at /firmware/. Page logs POSTed to /__log are
appended to a file so they can be read while a flash is running.

Usage: devserver.py [LOG_FILE]
"""
import functools
import http.server
import os
import sys

PAGE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(PAGE_DIR, '..', 'marvell_flash_tool')
FIRMWARE_DIR = os.path.join(PAGE_DIR, '..', 'firmware')
LOG_FILE = sys.argv[1] if len(sys.argv) > 1 else '/tmp/webusb.log'

# Keep in sync with BUNDLED_IMAGES in index.html and .github/workflows/pages.yml.
BUNDLED_IMAGES = [
    'bcm_erom.bin.usb', 'sysinit.img', 'bootloader.img', 'drm_erom.img',
    '08_IMAGE', '09_IMAGE', '79_IMAGE',
]


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        url_path = path.split('?', 1)[0].split('#', 1)[0]
        if url_path.startswith('/images/'):
            name = url_path[len('/images/'):]
            # Unknown names map to a path that does not exist, giving a 404.
            return os.path.join(IMAGE_DIR, name if name in BUNDLED_IMAGES else '.not-bundled')
        if url_path.startswith('/firmware/'):
            return os.path.join(FIRMWARE_DIR, os.path.basename(url_path))
        return super().translate_path(path)

    def do_POST(self):
        if self.path != '/__log':
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        with open(LOG_FILE, 'ab') as f:
            f.write(body)
        self.send_response(204)
        self.end_headers()


if __name__ == '__main__':
    handler = functools.partial(Handler, directory=PAGE_DIR)
    http.server.ThreadingHTTPServer(('127.0.0.1', 8000), handler).serve_forever()
