import os
from pathlib import Path
import math
from enum import Enum
from flags import Flags
from time import strftime, localtime

class SuperblockFlags(Flags):
    UNCOMPRESSED_INODES = 0x0001
    UNCOMPRESSED_DATA = 0x0002
    CHECK = 0x0004
    UNCOMPRESSED_FRAGMENTS = 0x0008
    NO_FRAGMENTS = 0x0010
    ALWAYS_FRAGMENTS = 0x0020
    DUPLICATES = 0x0040
    EXPORTABLE = 0x0080
    UNCOMPRESSED_XATTRS = 0x0100
    NO_XATTRS = 0x0200
    COMPRESSOR_OPTIONS = 0x0400
    UNCOMPRESSED_IDS = 0x0800

class Compression(Enum):
    GZIP = 1
    LZMA = 2
    LZO = 3
    XZ = 4
    LZ4 = 5
    ZSTD = 6

def getNextNullByte(fileBytes, startIndex):
    while startIndex < len(fileBytes):
        if fileBytes[startIndex] == 0:
            return startIndex
        startIndex += 1
    return -1

stockPath = Path('filesystem.squashfs')

fileBytes = b''

with open(stockPath, 'rb') as file:
    fileBytes = file.read()

magic = fileBytes[0:4][::-1].hex()
inode_count = int.from_bytes(fileBytes[4:8], 'little')
modification_time = int.from_bytes(fileBytes[8:12], 'little')
block_size = int.from_bytes(fileBytes[12:16], 'little')
fragment_entry_count = int.from_bytes(fileBytes[16:20], 'little')
compression_id = int.from_bytes(fileBytes[20:22], 'little')
block_log = int.from_bytes(fileBytes[22:24], 'little')
flags = int.from_bytes(fileBytes[24:26], 'little')
id_count = int.from_bytes(fileBytes[26:28], 'little')
version_major = int.from_bytes(fileBytes[28:30], 'little')
version_minor = int.from_bytes(fileBytes[30:32], 'little')
root_inode_ref = int.from_bytes(fileBytes[32:40], 'little')
bytes_used = int.from_bytes(fileBytes[40:48], 'little')
id_table_start = int.from_bytes(fileBytes[48:56], 'little')
xattr_id_table_start = int.from_bytes(fileBytes[56:64], 'little')
inode_table_start = int.from_bytes(fileBytes[64:72], 'little')
directory_table_start = int.from_bytes(fileBytes[72:80], 'little')
fragment_table_start = int.from_bytes(fileBytes[80:88], 'little')
export_table_start = int.from_bytes(fileBytes[88:96], 'little')

print('magic', magic)
print('inode_count', inode_count)
print('modification_time', strftime('%Y-%m-%d %H:%M:%S', localtime(modification_time)))
print('block_size', block_size)
print('fragment_entry_count', fragment_entry_count)
print('compression_id', Compression(compression_id).name)
print('block_log', block_log)
print('flags', SuperblockFlags(flags))
print('id_count', id_count)
print('version_major', version_major)
print('version_minor', version_minor)
print('root_inode_ref', root_inode_ref)
print('bytes_used', bytes_used)
print('id_table_start', id_table_start)
print('xattr_id_table_start', xattr_id_table_start)
print('inode_table_start', inode_table_start)
print('directory_table_start', directory_table_start)
print('fragment_table_start', fragment_table_start)
print('export_table_start', export_table_start)

# Asserting the magic number

assert magic == '73717368', f"Invalid magic number: {magic}. Expected '73717368'"

# Asserting the inode count
assert inode_count > 0, f"Invalid inode count: {inode_count}. Expected greater than 0"

# Asserting the block size
assert block_size > 0, f"Invalid block size: {block_size}. Expected greater than 0"

# Asserting the compression ID
assert compression_id in [0, 1, 2, 3, 4, 5, 6], f"Invalid compression ID: {compression_id}. Expected 0 or 1"

# Asserting the block log
assert math.log(block_size,2) == block_log, f"Invalid block log: {block_log}. Expected log2(block_size)"
