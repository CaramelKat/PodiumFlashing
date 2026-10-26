// Writes Wi-Fi and device name defaults into an 83_IMAGE without unpacking it.
//
// LibreEnv seeds its settings from /caldata/FENV.bin (the factory environment) inside the rootfs squashfs. Values
// stored in CENV (the setup app's writes) override it. This module replaces that file's ssid, security and
// passphrase items (when an ssid is given) and its FriendlyName (device name) item.
//
// Squashfs metadata is compressed, so editing it in place would need the recompressed bytes to fit exactly.
// Instead everything that changes is appended after the existing data as uncompressed blocks, and the superblock's
// root inode is pointed at a copy of the root directory whose chain (root -> caldata -> FENV.bin) leads to the
// appended copies. Nothing already in the image moves. The region table's size and CRC32 are updated.
//
// Usage: WifiPatch.patchImage(blob, { ssid, security, passphrase, deviceName }, onProgress) -> Promise<Blob>
(function () {
  'use strict';

  const { Squashfs, view, concat, serializeDir, dirInode, fileInode, metadataBlocks, metadataSize, withEntry } =
    typeof module !== 'undefined' ? require('./squashfs') : globalThis.SquashfsLib;

  const IMAGE_MAGIC = [0xf1, 0xa3, 0xad, 0xd2];
  const HEADER_SIZE = 64, ENTRY_SIZE = 64, DATA_OFFSET = 640;
  const NAND_BLOCK = 0x20000;
  const ITEM_IDS = { ssid: 2, security: 3, passphrase: 4, deviceName: 0x0b };

  const CRC_TABLE = (() => {
    const table = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[n] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes, crc = 0) {
    crc = ~crc >>> 0;
    for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    return ~crc >>> 0;
  }

  // ---- FENV.bin: "FENV", u16 body length, 2 bytes, u32 CRC-32 of the body, then <id><value>\0 items ----

  function parseFenv(bytes) {
    const dv = view(bytes);
    if (String.fromCharCode(...bytes.subarray(0, 4)) !== 'FENV') throw new Error('FENV.bin has an unexpected format');
    const length = dv.getUint16(4, true);
    const body = bytes.subarray(12, 12 + length);
    if (crc32(body) !== dv.getUint32(8, true)) throw new Error('FENV.bin CRC does not match its contents');
    const items = new Map();
    for (let i = 0; i < body.length;) {
      let end = i + 1;
      while (end < body.length && body[end] !== 0) end++;
      items.set(body[i], body.slice(i + 1, end));
      i = end + 1;
    }
    return { extra: bytes.slice(6, 8), items };
  }

  function buildFenv({ extra, items }) {
    const body = concat([...[...items.keys()].sort((a, b) => a - b)]
      .map(id => concat([Uint8Array.of(id), items.get(id), Uint8Array.of(0)])));
    const out = new Uint8Array(12 + body.length);
    const dv = view(out);
    out.set([0x46, 0x45, 0x4e, 0x56], 0);
    dv.setUint16(4, body.length, true);
    out.set(extra, 6);
    dv.setUint32(8, crc32(body), true);
    out.set(body, 12);
    return out;
  }

  function applySettings(fenv, settings) {
    const parsed = parseFenv(fenv);
    const encode = new TextEncoder();
    if (settings.ssid) {
      parsed.items.set(ITEM_IDS.ssid, encode.encode(settings.ssid));
      parsed.items.set(ITEM_IDS.security, encode.encode(settings.security));
      parsed.items.set(ITEM_IDS.passphrase, encode.encode(settings.passphrase));
    }
    if (settings.deviceName) parsed.items.set(ITEM_IDS.deviceName, encode.encode(settings.deviceName));
  return buildFenv(parsed);
  }

  // Returns the appended bytes and the new superblock for a rootfs that has `settings` baked into FENV.bin.
  async function patchRootfs(fs, settings) {
    const root = await fs.inode(fs.rootRef);
    const rootEntries = await fs.directory(root);
    const caldataEntry = rootEntries.find(e => e.name === 'caldata');
    if (!caldataEntry) throw new Error('/caldata not found in the rootfs');
    const caldata = await fs.inode(caldataEntry.start * 65536 + caldataEntry.offset);
    const calEntries = await fs.directory(caldata);
    const fenvEntry = calEntries.find(e => e.name === 'FENV.bin');
    if (!fenvEntry) throw new Error('/caldata/FENV.bin not found in the rootfs');
    const fenvNode = await fs.inode(fenvEntry.start * 65536 + fenvEntry.offset);
    const fenv = applySettings(await fs.fileContents(fenvNode), settings);

    // Layout of what is appended: FENV data, one inode block (file, caldata, root), caldata dir, root dir.
    const dataAt = Math.ceil(fs.size / 4) * 4;
    const inodesAt = dataAt + fenv.length;
    const inodeBytes = 36 + 32 + 32;
    const inodeBlock = inodesAt - fs.inodeTable;
    const newCal = withEntry(calEntries, 'FENV.bin', { start: inodeBlock, offset: 0 });
    const newRoot = withEntry(rootEntries, 'caldata', { start: inodeBlock, offset: 36 });
    const calDir = serializeDir(newCal), rootDir = serializeDir(newRoot);
    const calDirAt = inodesAt + metadataSize(inodeBytes);
    const rootDirAt = calDirAt + metadataSize(calDir.length);
    const end = rootDirAt + metadataSize(rootDir.length);

    // squashfs-tools finds the end of each table from the first block of the one after it, so the tables after the
    // directory table (fragment index, lookup table, id table) must stay contiguous and end the file. The whole tail
    // is copied behind the appended data; the lookup and id indexes are rebased onto their moved blocks, while the
    // fragment index still points at the fragment blocks left in place.
    const tail = await fs.read(fs.fragTable, fs.bytesUsed - fs.fragTable);
    const tailAt = end;
    const delta = BigInt(tailAt - fs.fragTable);
    const tdv = view(tail);
    const rebase = (at, entries) => {
      for (let i = 0; i < entries; i++) {
        const pos = at - fs.fragTable + 8 * i;
        tdv.setBigUint64(pos, tdv.getBigUint64(pos, true) + delta, true);
      }
    };
    rebase(fs.lookupTable, Math.ceil(fs.inodes / 1024));
    rebase(fs.idTable, Math.ceil(fs.ids / 2048));
    const total = tailAt + tail.length;

    const appended = concat([
      fenv,
      metadataBlocks(concat([
        fileInode(fenvNode, dataAt, fenv.length),
        dirInode(caldata, calDirAt - fs.dirTable, calDir.length),
        dirInode(root, rootDirAt - fs.dirTable, rootDir.length),
      ])),
      metadataBlocks(calDir),
      metadataBlocks(rootDir),
      tail,
    ]);
    if (dataAt + appended.length !== total) throw new Error('internal error: appended size mismatch');

    const superblock = fs.superblock.slice();
    const sdv = view(superblock);
    sdv.setBigUint64(32, BigInt(inodeBlock) * 65536n + 68n, true);
    sdv.setBigUint64(40, BigInt(total), true);
    sdv.setBigUint64(80, BigInt(tailAt), true);
    sdv.setBigUint64(88, BigInt(fs.lookupTable) + delta, true);
    sdv.setBigUint64(48, BigInt(fs.idTable) + delta, true);
    return { appended, pad: dataAt - fs.size, superblock };
  }

  // ---- 83_IMAGE container ----

  function validate(settings) {
    const bytes = text => new TextEncoder().encode(text).length;
    if (!settings.ssid && !settings.deviceName) throw new Error('Nothing to change');
    if (settings.deviceName && bytes(settings.deviceName) > 63) throw new Error('The device name must be 1 to 63 bytes');
    if (!settings.ssid) return;
    if (bytes(settings.ssid) > 32) throw new Error('The SSID must be 1 to 32 bytes');
    if (!['WPA-PSK', 'NONE'].includes(settings.security)) throw new Error('Unknown security type');
    if (settings.security === 'WPA-PSK' && (bytes(settings.passphrase) < 8 || bytes(settings.passphrase) > 63)) {
      throw new Error('A WPA passphrase must be 8 to 63 bytes');
    }
    if (settings.security === 'NONE' && settings.passphrase) throw new Error('An open network has no passphrase');
  }

  async function blobCrc(blob, onProgress) {
    let crc = 0;
    const chunk = 8 << 20;
    for (let at = 0; at < blob.size; at += chunk) {
      crc = crc32(new Uint8Array(await blob.slice(at, at + chunk).arrayBuffer()), crc);
      if (onProgress) onProgress(Math.min(at + chunk, blob.size), blob.size);
    }
    return crc;
  }

  async function patchImage(blob, settings, onProgress) {
    validate(settings);
    const head = new Uint8Array(await blob.slice(0, DATA_OFFSET).arrayBuffer());
    if (!IMAGE_MAGIC.every((b, i) => head[i] === b)) throw new Error('This is not an 83_IMAGE');
    const dv = view(head);
    let offset = DATA_OFFSET, rootfs = null;
    for (let i = 0; i < head[28]; i++) {
      const entry = HEADER_SIZE + i * ENTRY_SIZE;
      const name = new TextDecoder().decode(head.subarray(entry, entry + 16)).split('\0')[0];
      const size = dv.getUint32(entry + 16, true);
      if (name === 'rootfs') rootfs = { entry, offset, size, capacity: dv.getUint16(entry + 44, true) * NAND_BLOCK };
      offset += size;
    }
    if (!rootfs) throw new Error('The image has no rootfs region');

    const fs = new Squashfs(blob, rootfs.offset, rootfs.size);
    await fs.init();
    const { appended, pad, superblock } = await patchRootfs(fs, settings);
    const size = rootfs.size + pad + appended.length;
    if (size > rootfs.capacity) throw new Error('The patched rootfs no longer fits its partition');

    const payload = new Blob([
      superblock,
      blob.slice(rootfs.offset + superblock.length, rootfs.offset + rootfs.size),
      new Uint8Array(pad),
      appended,
    ]);
    dv.setUint32(rootfs.entry + 16, payload.size, true);
    dv.setUint32(rootfs.entry + 24, await blobCrc(payload, onProgress), true);
    return new Blob([head, blob.slice(DATA_OFFSET, rootfs.offset), payload, blob.slice(rootfs.offset + rootfs.size)]);
  }

  const api = { patchImage, parseFenv, buildFenv, crc32 };
  if (typeof module !== 'undefined') module.exports = api;
  else globalThis.WifiPatch = api;
})();
