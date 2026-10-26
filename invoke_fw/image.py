"""The Invoke 83_IMAGE firmware container: parse, verify, edit regions and rebuild.

An image is:

    [64-byte header][region table, 64 bytes per region][padding up to offset 640][payloads, back to back]

Each region table entry holds the region name, payload size, CRC32 and the NAND partition it is flashed to.
"""
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b'\xf1\xa3\xad\xd2'
BLOCK_SIZE = 131072          # NAND erase block (128 KiB)
HEADER_SIZE = 64
REGION_ENTRY_SIZE = 64
IMAGE_DATA_OFFSET = 640
FOOTER = 'footer'


class ImageError(Exception):
    """Raised when the file is not a valid 83_IMAGE or an edit does not fit."""


@dataclass
class Region:
    """One payload in the image (rootfs, bootloader, ...)."""
    name: str
    offset: int          # offset of the payload inside the image file
    size: int            # actual payload size
    crc32: int           # expected CRC32 of the payload (0 for footer)
    start_block: int     # NAND partition start (128 KiB blocks)
    block_count: int     # allocated partition size (128 KiB blocks)
    data: bytes = b''
    entry: bytes = field(default=b'', repr=False)   # the raw 64-byte table entry (empty for footer)

    @property
    def capacity(self):
        return self.block_count * BLOCK_SIZE

    @property
    def crc_ok(self):
        return zlib.crc32(self.data) == self.crc32


@dataclass
class FirmwareImage:
    """A parsed image: the raw header plus its regions."""
    header: bytes
    regions: list
    padding: bytes = b''   # unused bytes between the region table and the first payload

    @property
    def partitions(self):
        return [r for r in self.regions if r.name != FOOTER]

    def get(self, name):
        return next((r for r in self.regions if r.name == name), None)

    def verify(self):
        """Return a list of problems (empty if the image is sane)."""
        problems = []
        for r in self.partitions:
            if len(r.data) != r.size:
                problems.append(f'{r.name}: truncated payload')
            elif not r.crc_ok:
                problems.append(f'{r.name}: CRC mismatch')
            if r.size > r.capacity:
                problems.append(f'{r.name}: larger than its partition')
        return problems

    def replace(self, name, data):
        """Swap a region's payload for `data`, updating its size and CRC32. Raises ImageError if it does not fit."""
        region = self.get(name)
        if region is None:
            raise ImageError(f'no region called {name!r}')
        if region.name != FOOTER and len(data) > region.capacity:
            raise ImageError(f'{name} is {len(data)} bytes but its partition holds {region.capacity}')
        region.data = bytes(data)
        region.size = len(data)
        if region.name != FOOTER:
            region.crc32 = zlib.crc32(data)
            region.entry = (region.entry[:16] + region.size.to_bytes(4, 'little') + region.entry[20:24]
                            + region.crc32.to_bytes(4, 'little') + region.entry[28:])
        offset = IMAGE_DATA_OFFSET
        for r in self.regions:
            r.offset = offset
            offset += r.size

    def to_bytes(self):
        table = b''.join(r.entry for r in self.partitions)
        return self.header + table + self.padding + b''.join(r.data for r in self.regions)

    def save(self, path):
        Path(path).write_bytes(self.to_bytes())

    def extract_to(self, directory, name=None):
        """Write every region (or just `name`) to <directory>/<region>.bin."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        regions = [self.get(name)] if name else self.regions
        if None in regions:
            raise ImageError(f'no region called {name!r}')
        for r in regions:
            (directory / f'{r.name}.bin').write_bytes(r.data)

    def load_dir(self, directory):
        """Replace every region that has a <directory>/<region>.bin file; returns the names that changed."""
        changed = []
        for r in self.regions:
            path = Path(directory) / f'{r.name}.bin'
            if not path.exists():
                raise ImageError(f'missing {path}')
            if path.read_bytes() != r.data:
                self.replace(r.name, path.read_bytes())
                changed.append(r.name)
        return changed

    def table(self):
        """A human-readable table of the regions."""
        mb = lambda n: f'({n / (1 << 20):.3f} MB)'
        rows = ['| Name             | Img Size             | CRC      | Start | Partition Size   |',
                '| ---------------- + -------------------- + -------- + ----- + ---------------- |']
        for r in self.regions:
            rows.append('| {:16s} | {:8d} {:11s} | {:08x} |  {:4d} | {:4d} {:11s} |'.format(
                r.name, r.size, mb(r.size), r.crc32, r.start_block, r.block_count, mb(r.capacity)))
        rows.append('| ---------------- | -------------------- | -------- | ----- | ---------------- |')
        return '\n'.join(rows)


def parse_image(raw: bytes) -> FirmwareImage:
    """Split the raw bytes of an 83_IMAGE into regions."""
    if len(raw) < IMAGE_DATA_OFFSET or raw[:4] != MAGIC:
        raise ImageError("Magic key doesn't match! Unknown image format!")
    count = raw[28]
    table_end = HEADER_SIZE + count * REGION_ENTRY_SIZE
    if table_end > IMAGE_DATA_OFFSET:
        raise ImageError(f'Too many regions: {count}')
    regions = []
    data_off = IMAGE_DATA_OFFSET
    for i in range(count):
        o = HEADER_SIZE + i * REGION_ENTRY_SIZE
        e = raw[o:o + REGION_ENTRY_SIZE]
        size = int.from_bytes(e[16:20], 'little')
        regions.append(Region(
            name=e[0:16].split(b'\0')[0].decode('utf-8'),
            offset=data_off,
            size=size,
            crc32=int.from_bytes(e[24:28], 'little'),
            start_block=int.from_bytes(e[40:42], 'little'),
            block_count=int.from_bytes(e[44:46], 'little'),
            data=raw[data_off:data_off + size],
            entry=bytes(e)))
        data_off += size
    regions.append(Region(FOOTER, data_off, max(len(raw) - data_off, 0), 0, 0, 0, raw[data_off:]))
    return FirmwareImage(raw[:HEADER_SIZE], regions, raw[table_end:IMAGE_DATA_OFFSET])


def load_image(path) -> FirmwareImage:
    """Read and parse an 83_IMAGE file."""
    return parse_image(Path(path).read_bytes())
