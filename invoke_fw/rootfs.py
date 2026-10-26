"""Helpers for the squashfs root filesystem inside an image: superblock info and unpack/pack with squashfs-tools."""
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .image import BLOCK_SIZE

COMPRESSION = {1: 'gzip', 2: 'lzma', 3: 'lzo', 4: 'xz', 5: 'lz4', 6: 'zstd'}
FLAGS = {
    0x0001: 'UNCOMPRESSED_INODES', 0x0002: 'UNCOMPRESSED_DATA', 0x0004: 'CHECK', 0x0008: 'UNCOMPRESSED_FRAGMENTS',
    0x0010: 'NO_FRAGMENTS', 0x0020: 'ALWAYS_FRAGMENTS', 0x0040: 'DUPLICATES', 0x0080: 'EXPORTABLE',
    0x0100: 'UNCOMPRESSED_XATTRS', 0x0200: 'NO_XATTRS', 0x0400: 'COMPRESSOR_OPTIONS', 0x0800: 'UNCOMPRESSED_IDS',
}


def superblock(data: bytes) -> dict:
    """Decode a squashfs 4 superblock; raises ValueError if `data` is not one."""
    if data[:4] != b'hsqs':
        raise ValueError('not a little-endian squashfs image')
    u = lambda a, b: int.from_bytes(data[a:b], 'little')
    info = {
        'inode_count': u(4, 8),
        'modification_time': datetime.fromtimestamp(u(8, 12)).strftime('%Y-%m-%d %H:%M:%S'),
        'block_size': u(12, 16),
        'fragment_entry_count': u(16, 20),
        'compression': COMPRESSION.get(u(20, 22), f'unknown ({u(20, 22)})'),
        'block_log': u(22, 24),
        'flags': [name for bit, name in FLAGS.items() if u(24, 26) & bit],
        'id_count': u(26, 28),
        'version': f'{u(28, 30)}.{u(30, 32)}',
        'root_inode_ref': u(32, 40),
        'bytes_used': u(40, 48),
        'id_table_start': u(48, 56),
        'xattr_id_table_start': u(56, 64),
        'inode_table_start': u(64, 72),
        'directory_table_start': u(72, 80),
        'fragment_table_start': u(80, 88),
        'export_table_start': u(88, 96),
    }
    if 1 << info['block_log'] != info['block_size']:
        raise ValueError(f"block_log {info['block_log']} does not match block_size {info['block_size']}")
    return info


TOOLS = {
    'unsquashfs': ('rootfs unpack (imager.py extract/flash)', True),
    'mksquashfs': ('rootfs pack (imager.py build/flash)', True),
    'ssh': ('cenv/fenv --device', False),
}


def check_tools():
    """Return [(tool, path or None, purpose, required)] for the external programs the tooling calls."""
    return [(name, shutil.which(name), purpose, required) for name, (purpose, required) in TOOLS.items()]


def _tool(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f'{name} not found; install squashfs-tools (on macOS run these inside Docker instead)')
    return path


def unpack(squashfs, directory):
    """unsquashfs a filesystem into `directory` (needs root to recreate device nodes)."""
    subprocess.run([_tool('unsquashfs'), '-f', '-d', str(directory), str(squashfs)], check=True)


def pack(directory, squashfs):
    """mksquashfs `directory` the way the firmware expects: 128 KiB blocks, gzip, no appending."""
    subprocess.run([_tool('mksquashfs'), str(directory), str(squashfs), '-b', str(BLOCK_SIZE), '-comp', 'gzip',
                    '-noappend'], check=True)
