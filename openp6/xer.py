"""Dependency-free reader/writer for Primavera P6 XER files.

The XER format is a tab-delimited text stream:
    ERMHDR <header metadata>
    %T <TABLE>            start a table
    %F <field> <field>... column names
    %R <val> <val>...     a data row (columns per the preceding %F)
    %E                    end of file
Files are Windows-1252 encoded with CRLF newlines.
"""
from __future__ import annotations

from pathlib import Path

ENCODING = "cp1252"


class Xer:
    """A lossless, table-oriented view of an XER file."""

    def __init__(self, header=None, fields=None, rows=None):
        self.header = header or []          # ERMHDR line, split on tabs
        self.fields = fields or {}          # table -> [field names]
        self.rows = rows or {}              # table -> [ {field: value} ]
        self.encoding = ENCODING            # set by load(); used by write()

    # ---- parsing -------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "Xer":
        header, fields, rows = [], {}, {}
        cur = None
        for line in text.splitlines():
            if not line.strip():
                continue
            tag = line.split("\t", 1)[0]
            if tag == "ERMHDR":
                header = line.split("\t")
            elif tag == "%T":
                cur = line.split("\t", 1)[1].strip()
                fields.setdefault(cur, [])
                rows.setdefault(cur, [])
            elif tag == "%F":
                fields[cur] = line.split("\t")[1:]
            elif tag == "%R":
                vals = line.split("\t")[1:]
                rec = {f: (vals[i] if i < len(vals) else "")
                       for i, f in enumerate(fields[cur])}
                # keep columns beyond the declared %F (rare, but don't lose data)
                for j in range(len(fields[cur]), len(vals)):
                    rec[f"_extra{j}"] = vals[j]
                rows[cur].append(rec)
            elif tag == "%E":
                break
        return cls(header, fields, rows)

    @classmethod
    def load(cls, path: str) -> "Xer":
        """Read an XER, auto-detecting UTF-8 vs Windows-1252.

        P6's documented encoding is cp1252, but real exports are often UTF-8.
        Decoding a UTF-8 file as cp1252 silently mangles accented text, so try
        UTF-8 first and fall back (cp1252 bytes are usually invalid UTF-8).
        """
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "utf-8"):
            try:
                xer = cls.parse(raw.decode(enc))
                xer.encoding = enc
                return xer
            except UnicodeDecodeError:
                continue
        xer = cls.parse(raw.decode(ENCODING, errors="replace"))
        xer.encoding = ENCODING
        return xer

    # ---- access --------------------------------------------------------
    def table(self, name: str):
        return self.rows.get(name, [])

    def has(self, name: str) -> bool:
        return name in self.rows

    def table_names(self):
        return list(self.fields.keys())

    # ---- writing -------------------------------------------------------
    def write(self, path: str) -> None:
        out = []
        if self.header:
            out.append("\t".join(self.header))
        for name, flds in self.fields.items():
            out.append("%T\t" + name)
            out.append("%F\t" + "\t".join(flds))
            for rec in self.rows.get(name, []):
                out.append("%R\t" + "\t".join(rec.get(f, "") for f in flds))
        out.append("%E")
        enc = getattr(self, "encoding", ENCODING)
        try:
            data = "\r\n".join(out) + "\r\n"
            data.encode(enc)
        except (UnicodeEncodeError, LookupError):
            enc = ENCODING
        with open(path, "w", encoding=enc, errors="replace", newline="") as fh:
            fh.write("\r\n".join(out) + "\r\n")
