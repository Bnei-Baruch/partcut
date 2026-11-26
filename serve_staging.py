"""Threaded HTTP server with Range support for previewing staging files (localhost only)."""
import functools
import sys
from http.server import ThreadingHTTPServer
from RangeHTTPServer import RangeRequestHandler

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
handler = functools.partial(RangeRequestHandler, directory="/opt/filer/parts/staging")
ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
