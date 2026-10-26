"""Read and edit the Invoke's factory environment (FENV.bin).
"""
import argparse
import struct
import sys
import zlib

from .cenv import ITEM_NAMES


def parse(data):
    if data[:4] != b"FENV":
        raise SystemExit("not an FENV image")
    length = struct.unpack_from("<H", data, 4)[0]
    body = data[12:12 + length]
    if zlib.crc32(body) != struct.unpack_from("<I", data, 8)[0]:
        print("warning: CRC does not match", file=sys.stderr)
    items, i = {}, 0
    while i < len(body):
        end = body.index(b"\0", i + 1)
        items[body[i]] = body[i + 1:end]
        i = end + 1
    return data[6:8], items


def build(extra, items):
    body = b"".join(bytes([k]) + v + b"\0" for k, v in sorted(items.items()))
    return b"FENV" + struct.pack("<H", len(body)) + extra + struct.pack("<I", zlib.crc32(body)) + body


def ident(text):
    names = {v.lower(): k for k, v in ITEM_NAMES.items()}
    return names[text.lower()] if text.lower() in names else int(text, 0)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m invoke_fw fenv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("command", choices=["list", "get", "set"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("-o", "--out", help="write here instead of back to FILE")
    args = ap.parse_args(argv)
    extra, items = parse(open(args.file, "rb").read())
    if args.command == "list":
        for k, v in sorted(items.items()):
            print(f"{k:#04x}  {ITEM_NAMES.get(k, ''):<24} {v.decode('latin1')!r}")
    elif args.command == "get":
        print(items[ident(args.rest[0])].decode("latin1"))
    else:
        items[ident(args.rest[0])] = args.rest[1].encode()
        with open(args.out or args.file, "wb") as f:
            f.write(build(extra, items))
