"""Loopback preview with staging dictionary URLs; never writes production data."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, staging, **kwargs):
        self.staging = staging
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        pathname = unquote(urlsplit(path).path)
        prefix = "/data/ja/"
        if pathname.startswith(prefix):
            resolved = (self.staging / pathname[len(prefix):]).resolve()
            if resolved.is_relative_to(self.staging):
                return str(resolved)
            return str(self.staging / "invalid-path")
        return super().translate_path(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--port", type=int, default=4174)
    args = parser.parse_args()
    staging = args.staging.resolve()
    if not (staging / "terms.min.json").is_file():
        parser.error("Missing staging terms index")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, staging=staging))
    print(f"Staging preview: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
