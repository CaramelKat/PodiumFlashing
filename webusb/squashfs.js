// Minimal squashfs 4.0 (gzip) reader plus the pieces needed to append replacement inodes, directories and metadata
// blocks to an existing image. See patch.js for how they are used.
//
// Works as a global script (globalThis.SquashfsLib) or as a Node module.
(function () {
  'use strict';

  const SQUASHFS_MAGIC = 0x73717368;
  const META_SIZE = 8192;
  const UNCOMPRESSED_BLOCK = 1 << 24;
  const NO_FRAGMENT = 0xffffffff;

  const view = bytes => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

  function concat(parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) {
      out.set(p, at);
      at += p.length;
    }
    return out;
  }

  async function inflate(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  class Squashfs {
    constructor(blob, base, size) {
      this.blob = blob;
      this.base = base;
      this.size = size;
    }

    async read(offset, length) {
      const start = this.base + offset;
      return new Uint8Array(await this.blob.slice(start, start + length).arrayBuffer());
    }

    async init() {
      const sb = await this.read(0, 96);
      const dv = view(sb);
      if (dv.getUint32(0, true) !== SQUASHFS_MAGIC) throw new Error('rootfs is not a squashfs image');
      if (dv.getUint16(28, true) !== 4) throw new Error('unsupported squashfs version');
      if (dv.getUint16(20, true) !== 1) throw new Error('rootfs is not gzip compressed');
      this.superblock = sb;
      this.blockSize = dv.getUint32(12, true);
      this.rootRef = Number(dv.getBigUint64(32, true));
      this.bytesUsed = Number(dv.getBigUint64(40, true));
      this.inodeTable = Number(dv.getBigUint64(64, true));
      this.dirTable = Number(dv.getBigUint64(72, true));
      this.fragTable = Number(dv.getBigUint64(80, true));
      this.idTable = Number(dv.getBigUint64(48, true));
      this.lookupTable = Number(dv.getBigUint64(88, true));
      this.ids = dv.getUint16(26, true);
      this.inodes = dv.getUint32(4, true);
      this.fragments = dv.getUint32(16, true);
    }

    // Reads `length` bytes of a metadata table starting inside the block that begins `block` bytes into the table.
    async metadata(tableStart, block, offset, length) {
      const out = new Uint8Array(length);
      let got = 0, pos = tableStart + block;
      while (got < length) {
        const header = view(await this.read(pos, 2)).getUint16(0, true);
        const size = header & 0x7fff;
        let data = await this.read(pos + 2, size);
        if (!(header & 0x8000)) data = await inflate(data);
        const take = Math.min(length - got, data.length - offset);
        if (take <= 0) throw new Error('squashfs metadata is truncated');
        out.set(data.subarray(offset, offset + take), got);
        got += take;
        offset = 0;
        pos += 2 + size;
      }
      return out;
    }

    async inode(ref) {
      const block = Math.floor(ref / 65536), offset = ref % 65536;
      const raw = await this.metadata(this.inodeTable, block, offset, 64);
      const dv = view(raw);
      const node = {
        type: dv.getUint16(0, true), mode: dv.getUint16(2, true), uid: dv.getUint16(4, true),
        gid: dv.getUint16(6, true), mtime: dv.getUint32(8, true), number: dv.getUint32(12, true),
      };
      if (node.type === 1) {
        Object.assign(node, {
          start: dv.getUint32(16, true), nlink: dv.getUint32(20, true), size: dv.getUint16(24, true),
          offset: dv.getUint16(26, true), parent: dv.getUint32(28, true),
        });
      } else if (node.type === 8) {
        Object.assign(node, {
          nlink: dv.getUint32(16, true), size: dv.getUint32(20, true), start: dv.getUint32(24, true),
          parent: dv.getUint32(28, true), offset: dv.getUint16(34, true),
        });
      } else if (node.type === 2) {
        Object.assign(node, {
          start: dv.getUint32(16, true), fragment: dv.getUint32(20, true), offset: dv.getUint32(24, true),
          size: dv.getUint32(28, true),
        });
      } else if (node.type === 9) {
        Object.assign(node, {
          start: Number(dv.getBigUint64(16, true)), size: Number(dv.getBigUint64(24, true)),
          fragment: dv.getUint32(44, true), offset: dv.getUint32(48, true),
        });
      } else {
        throw new Error(`unsupported squashfs inode type ${node.type}`);
      }
      return node;
    }

    async directory(node) {
      const bytes = await this.metadata(this.dirTable, node.start, node.offset, node.size - 3);
      const dv = view(bytes);
      const entries = [];
      for (let p = 0; p + 12 <= bytes.length;) {
        const count = dv.getUint32(p, true) + 1, start = dv.getUint32(p + 4, true), number = dv.getUint32(p + 8, true);
        p += 12;
        for (let i = 0; i < count; i++) {
          const length = dv.getUint16(p + 6, true) + 1;
          entries.push({
            start, offset: dv.getUint16(p, true), number: number + dv.getInt16(p + 2, true),
            type: dv.getUint16(p + 4, true), name: new TextDecoder().decode(bytes.subarray(p + 8, p + 8 + length)),
          });
          p += 8 + length;
        }
      }
      return entries;
    }

    async fileContents(node) {
      if (node.fragment === NO_FRAGMENT || node.size >= this.blockSize) {
        throw new Error('FENV.bin is not stored in a fragment as expected');
      }
      const index = view(await this.read(this.fragTable + 8 * Math.floor(node.fragment / 512), 8));
      const entries = Number(index.getBigUint64(0, true));
      const slot = (node.fragment % 512) * 16;
      const entry = view(await this.metadata(entries, 0, slot, 16));
      const start = Number(entry.getBigUint64(0, true)), sizeField = entry.getUint32(8, true);
      let block = await this.read(start, sizeField & 0xffffff);
      if (!(sizeField & UNCOMPRESSED_BLOCK)) block = await inflate(block);
      return block.slice(node.offset, node.offset + node.size);
    }
  }

  function serializeDir(entries) {
    const parts = [];
    for (let i = 0; i < entries.length;) {
      const first = entries[i];
      let j = i;
      while (j < entries.length && j - i < 256 && entries[j].start === first.start &&
             Math.abs(entries[j].number - first.number) < 0x7fff) j++;
      const header = new Uint8Array(12);
      const dv = view(header);
      dv.setUint32(0, j - i - 1, true);
      dv.setUint32(4, first.start, true);
      dv.setUint32(8, first.number, true);
      parts.push(header);
      for (const e of entries.slice(i, j)) {
        const name = new TextEncoder().encode(e.name);
        const raw = new Uint8Array(8 + name.length);
        const rv = view(raw);
        rv.setUint16(0, e.offset, true);
        rv.setInt16(2, e.number - first.number, true);
        rv.setUint16(4, e.type, true);
        rv.setUint16(6, name.length - 1, true);
        raw.set(name, 8);
        parts.push(raw);
      }
      i = j;
    }
    return concat(parts);
  }

  function inodeHeader(node, type, length) {
    const raw = new Uint8Array(length);
    const dv = view(raw);
    dv.setUint16(0, type, true);
    dv.setUint16(2, node.mode, true);
    dv.setUint16(4, node.uid, true);
    dv.setUint16(6, node.gid, true);
    dv.setUint32(8, node.mtime, true);
    dv.setUint32(12, node.number, true);
    return { raw, dv };
  }

  function dirInode(node, start, size) {
    const { raw, dv } = inodeHeader(node, 1, 32);
    dv.setUint32(16, start, true);
    dv.setUint32(20, node.nlink, true);
    dv.setUint16(24, size + 3, true);
    dv.setUint16(26, 0, true);
    dv.setUint32(28, node.parent, true);
    return raw;
  }

  function fileInode(node, dataStart, size) {
    const { raw, dv } = inodeHeader(node, 2, 36);
    dv.setUint32(16, dataStart, true);
    dv.setUint32(20, NO_FRAGMENT, true);
    dv.setUint32(24, 0, true);
    dv.setUint32(28, size, true);
    dv.setUint32(32, (size | UNCOMPRESSED_BLOCK) >>> 0, true);
    return raw;
  }

  // Wraps bytes in uncompressed metadata blocks of at most 8 KiB each.
  function metadataBlocks(bytes) {
    const parts = [];
    for (let i = 0; i < bytes.length; i += META_SIZE) {
      const chunk = bytes.subarray(i, i + META_SIZE);
      const header = new Uint8Array(2);
      view(header).setUint16(0, chunk.length | 0x8000, true);
      parts.push(header, chunk);
    }
    return concat(parts);
  }

  const metadataSize = length => length + 2 * Math.ceil(length / META_SIZE);

  function withEntry(entries, name, change) {
    const found = entries.find(e => e.name === name);
    if (!found) throw new Error(`/${name} not found in the rootfs`);
    return entries.map(e => (e === found ? { ...e, ...change } : e));
  }

  const api = {
    Squashfs, view, concat, serializeDir, dirInode, fileInode, metadataBlocks, metadataSize, withEntry,
  };
  if (typeof module !== 'undefined') module.exports = api;
  else globalThis.SquashfsLib = api;
})();
