"""Minimal IPP/1.1 client for talking to a CUPS server (standard library only).

Only what QuickPrint needs: list printers, list jobs, print a document,
cancel a job, resume (enable) a paused printer.
"""
import base64
import struct
import urllib.error
import urllib.request

# operation ids
PRINT_JOB = 0x0002
CANCEL_JOB = 0x0008
GET_JOBS = 0x000A
GET_PRINTER_ATTRIBUTES = 0x000B
RESUME_PRINTER = 0x0011
CUPS_GET_PRINTERS = 0x4002
CUPS_ACCEPT_JOBS = 0x4008

# delimiter tags
OP_ATTRS, JOB_ATTRS, END, PRINTER_ATTRS = 0x01, 0x02, 0x03, 0x04
# value tags
INTEGER, BOOLEAN, ENUM = 0x21, 0x22, 0x23
RANGE = 0x33
TEXT, NAME, KEYWORD, URI, CHARSET, LANG, MIME = 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49
_STRINGS = {0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x35, 0x36}

PAPER_SIZES = ["iso_a4_210x297mm", "iso_a3_297x420mm", "na_letter_8.5x11in",
               "na_legal_8.5x14in", "iso_a5_148x210mm"]

PRINTER_STATES = {3: "idle", 4: "printing", 5: "stopped"}
JOB_STATES = {3: "pending", 4: "held", 5: "printing", 6: "stopped",
              7: "canceled", 8: "aborted", 9: "completed"}


class IPPError(Exception):
    def __init__(self, message, status=None, http_status=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def _attr(tag, name, value):
    vals = value if isinstance(value, list) else [value]
    out = b""
    for i, v in enumerate(vals):
        n = name.encode() if i == 0 else b""
        if tag in (INTEGER, ENUM):
            data = struct.pack(">i", v)
        elif tag == BOOLEAN:
            data = b"\x01" if v else b"\x00"
        elif tag == RANGE:
            data = struct.pack(">ii", *v)
        else:
            data = str(v).encode("utf-8")
        out += struct.pack(">bH", tag, len(n)) + n + struct.pack(">H", len(data)) + data
    return out


def _decode_value(tag, data):
    if tag in (INTEGER, ENUM):
        return struct.unpack(">i", data)[0]
    if tag == BOOLEAN:
        return data != b"\x00"
    if tag == RANGE:
        return struct.unpack(">ii", data)
    if tag in _STRINGS:
        return data.decode("utf-8", "replace")
    return data  # dates, resolutions etc. are not needed


def _parse(body):
    status = struct.unpack(">H", body[2:4])[0]
    i = 8
    groups, cur, last = [], None, None
    while i < len(body):
        tag = body[i]
        i += 1
        if tag <= 0x0F:  # delimiter
            if tag == END:
                break
            cur = {}
            groups.append((tag, cur))
            continue
        nlen = struct.unpack(">H", body[i:i + 2])[0]
        name = body[i + 2:i + 2 + nlen].decode()
        i += 2 + nlen
        vlen = struct.unpack(">H", body[i:i + 2])[0]
        val = _decode_value(tag, body[i + 2:i + 2 + vlen])
        i += 2 + vlen
        if cur is None:
            continue
        if name:
            last = name
            cur[name] = val
        elif last:  # additional value of a multi-valued attribute
            prev = cur[last]
            cur[last] = (prev if isinstance(prev, list) else [prev]) + [val]
    return status, groups


class CupsClient:
    def __init__(self, host, port=631, admin_user=None, admin_password=None,
                 user="quickprint", timeout=30):
        self.base = f"http://{host}:{port}"
        self.host = host
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.user = user
        self.timeout = timeout
        self._req_id = 0

    @property
    def can_admin(self):
        return bool(self.admin_user and self.admin_password)

    def printer_uri(self, name):
        return f"ipp://{self.host}/printers/{name}"

    def _request(self, op, path, op_attrs, job_attrs=None, document=None, admin=False):
        self._req_id += 1
        body = struct.pack(">bbHi", 1, 1, op, self._req_id)
        body += bytes([OP_ATTRS])
        body += _attr(CHARSET, "attributes-charset", "utf-8")
        body += _attr(LANG, "attributes-natural-language", "en")
        for tag, name, value in op_attrs:
            body += _attr(tag, name, value)
        if job_attrs:
            body += bytes([JOB_ATTRS])
            for tag, name, value in job_attrs:
                body += _attr(tag, name, value)
        body += bytes([END])
        if document:
            body += document
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/ipp"})
        if admin:
            if not self.can_admin:
                raise IPPError("CUPS admin login is not configured on the server "
                               "(set CUPS_ADMIN_USER / CUPS_ADMIN_PASSWORD).", http_status=401)
            tok = base64.b64encode(f"{self.admin_user}:{self.admin_password}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise IPPError("CUPS refused the admin login.", http_status=e.code)
            raise IPPError(f"CUPS answered HTTP {e.code}.", http_status=e.code)
        except (urllib.error.URLError, OSError) as e:
            raise IPPError(f"Cannot reach the print server at {self.base} ({e}).")
        status, groups = _parse(raw)
        if status >= 0x0400:
            op_group = next((g for t, g in groups if t == OP_ATTRS), {})
            raise IPPError(op_group.get("status-message", f"IPP error 0x{status:04x}"), status=status)
        return groups

    # ---------- queries ----------
    def printers(self):
        groups = self._request(CUPS_GET_PRINTERS, "/", [
            (KEYWORD, "requested-attributes", [
                "printer-name", "printer-info", "printer-location", "printer-make-and-model",
                "printer-state", "printer-state-reasons", "printer-state-message",
                "printer-is-accepting-jobs", "queued-job-count", "device-uri",
                "color-supported", "sides-supported", "print-color-mode-supported",
                "media-supported", "media-default"]),
        ])
        out = []
        for tag, g in groups:
            if tag != PRINTER_ATTRS:
                continue
            reasons = g.get("printer-state-reasons", [])
            reasons = [r for r in (reasons if isinstance(reasons, list) else [reasons]) if r != "none"]
            sides = g.get("sides-supported", ["one-sided"])
            sides = sides if isinstance(sides, list) else [sides]
            modes = g.get("print-color-mode-supported", [])
            modes = modes if isinstance(modes, list) else [modes]
            media = g.get("media-supported", [])
            media = media if isinstance(media, list) else [media]
            out.append({
                "name": g.get("printer-name"),
                "info": g.get("printer-info") or g.get("printer-name"),
                "location": g.get("printer-location", ""),
                "model": g.get("printer-make-and-model", ""),
                "state": PRINTER_STATES.get(g.get("printer-state"), "unknown"),
                "reasons": reasons,
                "message": g.get("printer-state-message", ""),
                "accepting": g.get("printer-is-accepting-jobs", True),
                "queued": g.get("queued-job-count", 0),
                "device_uri": g.get("device-uri", ""),
                "color": bool(g.get("color-supported")) or "color" in modes,
                "duplex": any(s.startswith("two-sided") for s in sides),
                # the paper sizes people actually use, in this order
                "media": [m for m in PAPER_SIZES if m in media],
                "media_default": g.get("media-default", ""),
            })
        return out

    def jobs(self, printer, which="not-completed", limit=20):
        groups = self._request(GET_JOBS, "/", [
            (URI, "printer-uri", self.printer_uri(printer)),
            (NAME, "requesting-user-name", self.user),
            (INTEGER, "limit", limit),
            (KEYWORD, "which-jobs", which),
            (KEYWORD, "requested-attributes", [
                "job-id", "job-name", "job-originating-user-name", "job-state",
                "job-state-reasons", "job-printer-state-message", "job-media-sheets-completed",
                "time-at-creation", "time-at-completed", "copies", "job-k-octets"]),
        ])
        out = []
        for tag, g in groups:
            if tag != JOB_ATTRS or "job-id" not in g:
                continue
            reasons = g.get("job-state-reasons", [])
            out.append({
                "id": g["job-id"],
                "printer": printer,
                "name": g.get("job-name", ""),
                "user": g.get("job-originating-user-name", ""),
                "state": JOB_STATES.get(g.get("job-state"), "unknown"),
                "reasons": reasons if isinstance(reasons, list) else [reasons],
                "message": g.get("job-printer-state-message", ""),
                "created": g.get("time-at-creation"),
                "completed": g.get("time-at-completed"),
                "size_kb": g.get("job-k-octets"),
            })
        return out

    # ---------- actions ----------
    def print_document(self, printer, data, filename, copies=1, sides="one-sided",
                       color="color", page_ranges=None, orientation=None, fit=True, media=None):
        job = [(INTEGER, "copies", int(copies)),
               (KEYWORD, "sides", sides),
               (KEYWORD, "print-color-mode", "monochrome" if color == "grayscale" else "color")]
        if color == "grayscale":  # older PPD drivers look at ColorModel instead
            job.append((NAME, "ColorModel", "Gray"))
        if page_ranges:
            job.append((RANGE, "page-ranges", page_ranges))
        if orientation in (3, 4):
            job.append((ENUM, "orientation-requested", orientation))
        if fit:
            job.append((KEYWORD, "print-scaling", "fit"))
        if media:  # CUPS maps this to the driver's PageSize, the printer picks the tray
            job.append((KEYWORD, "media", media))
        groups = self._request(PRINT_JOB, f"/printers/{printer}", [
            (URI, "printer-uri", self.printer_uri(printer)),
            (NAME, "requesting-user-name", self.user),
            (NAME, "job-name", filename[:200]),
            (MIME, "document-format", "application/octet-stream"),
        ], job_attrs=job, document=data)
        for tag, g in groups:
            if tag == JOB_ATTRS and "job-id" in g:
                return g["job-id"]
        return None

    def cancel_job(self, printer, job_id):
        self._request(CANCEL_JOB, "/jobs", [
            (URI, "printer-uri", self.printer_uri(printer)),
            (INTEGER, "job-id", int(job_id)),
            (NAME, "requesting-user-name", self.user),
        ], admin=self.can_admin)

    def resume_printer(self, printer):
        """Enable a paused printer and make it accept jobs again (needs CUPS admin)."""
        attrs = [(URI, "printer-uri", self.printer_uri(printer)),
                 (NAME, "requesting-user-name", self.admin_user or self.user)]
        self._request(RESUME_PRINTER, "/admin/", attrs, admin=True)
        self._request(CUPS_ACCEPT_JOBS, "/admin/", attrs, admin=True)
