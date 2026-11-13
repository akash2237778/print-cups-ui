"""QuickPrint - a small web front-end for a CUPS print server.

Standard library only, so it runs on a stock python:3.11-slim image.

Environment:
  CUPS_SERVER          host of the CUPS server           (default localhost)
  CUPS_PORT            CUPS port                         (default 631)
  CUPS_ADMIN_USER      optional CUPS admin login - enables "Resume printer"
  CUPS_ADMIN_PASSWORD  and cancelling any job
  PORT                 port to listen on                 (default 8000)
  MAX_UPLOAD_MB        per-request upload limit          (default 50)
  PRINT_SHARE_HOST     address devices use to reach CUPS directly, e.g. the
                       NAS's Tailscale IP (default: host the page was opened on)
"""
import email.parser
import email.policy
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipp import CupsClient, IPPError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CUPS_SERVER = os.environ.get("CUPS_SERVER", "localhost")
CUPS_PORT = int(os.environ.get("CUPS_PORT", "631"))
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024

cups = CupsClient(CUPS_SERVER, CUPS_PORT,
                  admin_user=os.environ.get("CUPS_ADMIN_USER") or None,
                  admin_password=os.environ.get("CUPS_ADMIN_PASSWORD") or None)

SHARE_HOST = os.environ.get("PRINT_SHARE_HOST", "").strip()

PRINTABLE = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".txt", ".ps"}
OFFICE = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".rtf"}
SIDES = {"one-sided", "two-sided-long-edge", "two-sided-short-edge"}


def parse_pages(text):
    """'1-3, 5' -> [(1, 3), (5, 5)]; '' -> None."""
    text = (text or "").replace(" ", "")
    if not text:
        return None
    ranges = []
    for part in text.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"Page range '{text}' is not valid. Use e.g. 1-3,5")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b < a:
            raise ValueError(f"Page range '{part}' is not valid.")
        ranges.append((a, b))
    return ranges


def parse_multipart(content_type, body):
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((filename, payload))
        elif name:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def status_snapshot():
    printers = cups.printers()
    active, recent = [], []
    for p in printers:
        try:
            active += cups.jobs(p["name"], "not-completed", 50)
            recent += cups.jobs(p["name"], "completed", 10)
        except IPPError:
            pass
    for j in active + recent:  # unset times come back as bytes
        if not isinstance(j["completed"], int):
            j["completed"] = None
    active.sort(key=lambda j: j["id"])
    recent.sort(key=lambda j: j["completed"] or 0, reverse=True)
    return {"printers": printers, "active": active, "recent": recent[:12],
            "can_admin": cups.can_admin, "cups_url": f"http://{CUPS_SERVER}:{CUPS_PORT}"}


def airprint_profile(host, port, printers):
    """iOS/iPadOS configuration profile that adds the CUPS queues as AirPrint printers
    (needed because iPhones only discover printers via Bonjour, which doesn't cross Tailscale)."""
    from uuid import NAMESPACE_DNS, uuid5
    from xml.sax.saxutils import escape as x

    entries = "".join(
        "<dict><key>IPAddress</key><string>%s</string><key>Port</key><integer>%d</integer>"
        "<key>ResourcePath</key><string>printers/%s</string><key>ForceTLS</key><false/></dict>"
        % (x(host), port, x(p["name"])) for p in printers)
    inner = str(uuid5(NAMESPACE_DNS, f"airprint.{host}.quickprint"))
    outer = str(uuid5(NAMESPACE_DNS, f"profile.{host}.quickprint"))
    names = ", ".join(p["info"] for p in printers)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>PayloadContent</key><array><dict>
<key>AirPrint</key><array>{entries}</array>
<key>PayloadDisplayName</key><string>Office printers</string>
<key>PayloadIdentifier</key><string>local.quickprint.airprint.{inner}</string>
<key>PayloadType</key><string>com.apple.airprint</string>
<key>PayloadUUID</key><string>{inner}</string>
<key>PayloadVersion</key><integer>1</integer>
</dict></array>
<key>PayloadDescription</key><string>Adds the office printers ({x(names)}) to the Print menu. Tailscale must be connected to print.</string>
<key>PayloadDisplayName</key><string>Office printers (QuickPrint)</string>
<key>PayloadIdentifier</key><string>local.quickprint.profile.{outer}</string>
<key>PayloadRemovalDisallowed</key><false/>
<key>PayloadType</key><string>Configuration</string>
<key>PayloadUUID</key><string>{outer}</string>
<key>PayloadVersion</key><integer>1</integer>
</dict></plist>
""".encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "QuickPrint/2"

    def log_message(self, fmt, *args):
        if "/api/status" not in (args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _share_host(self):
        if SHARE_HOST:
            return SHARE_HOST
        host = (self.headers.get("Host") or CUPS_SERVER).strip()
        return host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]

    STATIC_TYPES = {".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css"}

    def _static(self, path):
        root = os.path.join(HERE, "static")
        full = os.path.realpath(os.path.join(root, unquote(path[len("/static/"):])))
        ctype = self.STATIC_TYPES.get(os.path.splitext(full)[1])
        if not ctype or not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full):
            return self._error(404, "Not found")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path in ("/setup", "/setup/"):
                with open(os.path.join(HERE, "setup.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/share":
                return self._send(200, {"host": self._share_host(), "port": CUPS_PORT,
                                        "printers": [{"name": p["name"], "info": p["info"], "model": p["model"]}
                                                     for p in cups.printers()]})
            if path == "/setup/office-printers.mobileconfig":
                data = airprint_profile(self._share_host(), CUPS_PORT, cups.printers())
                self.send_response(200)
                self.send_header("Content-Type", "application/x-apple-aspen-config")
                self.send_header("Content-Disposition", 'attachment; filename="office-printers.mobileconfig"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path.startswith("/static/"):
                return self._static(path)
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if path == "/api/status":
                return self._send(200, status_snapshot())
            if path == "/printers":  # kept for old scripts
                return self._send(200, {"printers": [p["name"] for p in cups.printers()]})
            return self._error(404, "Not found")
        except IPPError as e:
            return self._error(502, str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path in ("/api/print", "/print"):
                return self._print()
            m = re.fullmatch(r"/api/jobs/(\d+)/cancel", path)
            if m:
                body = self._json_body()
                cups.cancel_job(body.get("printer", ""), int(m.group(1)))
                return self._send(200, {"message": f"Job {m.group(1)} cancelled."})
            m = re.fullmatch(r"/api/printers/([^/]+)/resume", path)
            if m:
                name = unquote(m.group(1))
                cups.resume_printer(name)
                return self._send(200, {"message": f"{name} resumed."})
            return self._error(404, "Not found")
        except IPPError as e:
            msg = str(e)
            if "not authorized" in msg.lower() or "not-authorized" in msg.lower() or "forbidden" in msg.lower():
                msg = "Only jobs sent from QuickPrint can be cancelled here (or set an admin login)."
            return self._error(e.http_status if e.http_status in (401, 403) else 502, msg)
        except ValueError as e:
            return self._error(400, str(e))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, "Unexpected server error - see the app log.")

    def _print(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._error(413, f"Upload too large (limit {MAX_UPLOAD // 1048576} MB).")
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._error(400, "Expected a file upload.")
        fields, files = parse_multipart(ctype, self.rfile.read(length))
        if not files:
            return self._error(400, "No file was attached.")
        printer = fields.get("printer", "")
        pinfo = next((p for p in cups.printers() if p["name"] == printer), None)
        if not pinfo:
            return self._error(400, "Please choose a printer.")
        media = fields.get("media") or None
        if media and media not in pinfo["media"]:
            return self._error(400, "That paper size isn't available on this printer.")

        copies = max(1, min(99, int(fields.get("copies") or 1)))
        sides = fields.get("sides", "one-sided")
        sides = sides if sides in SIDES else "one-sided"
        color = "grayscale" if fields.get("color_mode") == "grayscale" else "color"
        pages = parse_pages(fields.get("pages"))
        orientation = {"portrait": 3, "landscape": 4}.get(fields.get("orientation"))
        fit = fields.get("fit", "1") != "0"

        results = []
        for filename, data in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext in OFFICE:
                results.append({"file": filename, "ok": False,
                                "error": "Word/Excel files can't be printed directly - "
                                         "use File > Save as PDF, then upload the PDF."})
                continue
            if ext not in PRINTABLE:
                results.append({"file": filename, "ok": False,
                                "error": "Unsupported file type. Use PDF, an image or a .txt file."})
                continue
            if not data:
                results.append({"file": filename, "ok": False, "error": "File is empty."})
                continue
            try:
                job_id = cups.print_document(printer, data, filename, copies, sides, color,
                                             pages if ext == ".pdf" else None, orientation, fit, media)
                results.append({"file": filename, "ok": True, "job_id": job_id})
            except IPPError as e:
                results.append({"file": filename, "ok": False, "error": str(e)})
        ok = any(r["ok"] for r in results)
        return self._send(200 if ok else 422, {"results": results})


def main():
    port = int(os.environ.get("PORT", "8000"))
    print(f"QuickPrint on :{port} -> CUPS {CUPS_SERVER}:{CUPS_PORT} "
          f"(admin {'on' if cups.can_admin else 'off'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
