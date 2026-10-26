"""Read and edit the Invoke's persistent config store (inherits from fenv)
"""
import argparse
import subprocess
import sys

PAGE = 2048
SIZE = 128 * 1024
MAGIC = b"CENV"
DEFAULT_FLAG = 0xFE
NAMES = {"ssid": 0x02, "security": 0x03, "password": 0x04, "name": 0x0B}
# CENV id -> item name, taken from LibreEnv's table (which numbers items from 1, one above the on-flash id)
ITEM_NAMES = {
    0x01: "SpotifyEnabled", 0x02: "ssid", 0x03: "security",
    0x04: "passphrase", 0x05: "netif", 0x06: "fw_method",
    0x07: "staticip", 0x08: "staticipaddr", 0x09: "fw_upgrade",
    0x0a: "spotify_log", 0x0b: "FriendlyName", 0x0c: "FwVersion",
    0x0d: "MCUVersion", 0x0e: "CUSTVersion", 0x0f: "telnet",
    0x10: "hostpresent", 0x11: "onetouchurl", 0x12: "uuid",
    0x13: "AirplayPassword", 0x14: "wep_key_index", 0x15: "WACMode",
    0x16: "seednv", 0x17: "airplay", 0x18: "DDMSOOHmode",
    0x19: "autoip", 0x1a: "CastSetup", 0x1b: "MCULatency",
    0x1c: "activeinterface", 0x1d: "p2p_state", 0x1e: "ddms_BAND",
    0x1f: "zoneid", 0x20: "ddms_sp_type", 0x21: "fw_port",
    0x22: "SP_BLOB", 0x23: "SP_USERNAME", 0x24: "spotifyPid",
    0x25: "factory_reset", 0x26: "current_volume", 0x27: "LRCK",
    0x28: "boot_after_factory", 0x29: "WAC_SSID", 0x2a: "ACPpresent",
    0x2b: "Model", 0x2c: "Manufacturer", 0x2d: "sdcard_playindex",
    0x2e: "ddms_password", 0x2f: "MCLK", 0x30: "xmodem_pkt_size",
    0x31: "AirPlayMetaData", 0x32: "BTCLK", 0x33: "ControllerID",
    0x34: "ControllerPublicKey", 0x35: "HKAccessoryPassword", 0x36: "HKAccessoryUUID",
    0x37: "ddms_channel", 0x38: "SPT_Preset1", 0x39: "SPT_Preset2",
    0x3a: "SPT_Preset3", 0x3b: "SPT_Preset1_Size", 0x3c: "SPT_Preset2_Size",
    0x3d: "SPT_Preset3_Size", 0x3e: "dmrPId", 0x3f: "SPT_DEINT",
    0x40: "spotVol", 0x41: "ddms_rate", 0x42: "AcpToLS",
    0x43: "DNS", 0x44: "BT_CONTROLLER", 0x45: "concurrent_SSID",
    0x46: "QPlay_MID", 0x47: "QPlay_HashKey", 0x48: "LEDControl",
    0x49: "netmask", 0x4a: "gateway", 0x4b: "primdns",
    0x4c: "secdns", 0x4d: "ddms_stream_type", 0x4e: "LibreManagerPId",
    0x4f: "ddms_security", 0x50: "ddms_encryption", 0x51: "LSHOST",
    0x52: "AutoWac", 0x53: "SDDPEnable", 0x54: "SDDPVersion",
    0x55: "SDDPType", 0x56: "SDDPPrimaryProxy", 0x57: "SDDPProxies",
    0x58: "SDDPDriver", 0x59: "SDDPMaxAge", 0x5a: "SDDPConfig_URL",
    0x5b: "AirPlay_TestDelay", 0x5c: "PlayerLatency", 0x5d: "BT_DeviceName",
    0x5e: "SpotifyAppkey", 0x5f: "Country", 0x60: "DLNA_ConnClosed",
    0x61: "HOST_BAUDRATE", 0x62: "fwupdate_link", 0x63: "fwdownload_xml",
    0x64: "AlbumArtMaxSizeKB", 0x65: "Serial_num", 0x66: "Model_num",
    0x67: "Hardware_version", 0x68: "Firmware_version", 0x69: "HTTPHost",
    0x6a: "DeezerUserName", 0x6b: "DeezerUserPassword", 0x6c: "ExternalDAC",
    0x6d: "TidalUserName", 0x6e: "TidalUserPassword", 0x6f: "PlayerState",
    0x70: "SpotifyDDMSMasterName", 0x71: "LuciTcpConnectionLimit", 0x72: "LED_RGB",
    0x73: "LED_INTENSITY", 0x74: "LED_FLASHING", 0x75: "LED_DEVICE",
    0x76: "LED_AMBER", 0x77: "LED_WHITE", 0x78: "GEN_FAV_0",
    0x79: "GEN_FAV_1", 0x7a: "GEN_FAV_2", 0x7b: "GEN_FAV_3",
    0x7c: "GEN_FAV_4", 0x7d: "GEN_FAV_5", 0x7e: "GEN_FAV_6",
    0x7f: "GEN_FAV_7", 0x80: "GEN_FAV_8", 0x81: "GEN_FAV_9",
    0x82: "LastPlayedURL", 0x83: "disable_eth_phy", 0x84: "append_macid",
    0x85: "HN_SSID_0", 0x86: "HN_SSID_1", 0x87: "HN_SSID_2",
    0x88: "HN_SSID_3", 0x89: "HN_SSID_4", 0x8a: "HN_SSID_5",
    0x8b: "HN_SSID_6", 0x8c: "HN_SSID_7", 0x8d: "HN_PASSPHRASE_0",
    0x8e: "HN_PASSPHRASE_1", 0x8f: "HN_PASSPHRASE_2", 0x90: "HN_PASSPHRASE_3",
    0x91: "HN_PASSPHRASE_4", 0x92: "HN_PASSPHRASE_5", 0x93: "HN_PASSPHRASE_6",
    0x94: "HN_PASSPHRASE_7", 0x95: "MultipleSSIDEnabled", 0x96: "HostUiEnabled",
    0x97: "DirectPrefixSet", 0x98: "Scene_Name", 0x99: "HostIP",
    0x9a: "I2S_Master", 0x9b: "RedirectionUrl", 0x9c: "NTP_Server",
    0x9d: "eastechcust", 0x9e: "DMRDisable", 0x9f: "BT_BAUDRATE",
    0xa0: "UART_Mode", 0xa1: "psm_timer_based_triggers", 0xa2: "psm_no_nw_pb_inact_tmr",
    0xa3: "psm_hn_nw_pb_inact_tmr", 0xa4: "psm_nw_stdby_inact_tmr", 0xa5: "OOH_SSID",
    0xa6: "3gbridging", 0xa7: "CloudServer", 0xa8: "CloudPort",
    0xa9: "fwupdate_success", 0xaa: "IPADDR", 0xab: "REMOTE_BD_ADDR",
    0xac: "otaupdate_link", 0xad: "cast_version", 0xae: "spdif",
    0xaf: "vTunerLoginURL", 0xb0: "vTunerLoginURLBackUp", 0xb1: "vTunerSearchURL",
    0xb2: "vTunerSearchURLBackup", 0xb3: "BlowfishKey", 0xb4: "BlowfishInitialVector",
    0xb5: "vTunerTokenURL", 0xb6: "vTunerTokenURLBackUp", 0xb7: "TIDALSoundQuality",
    0xb8: "CustomerId", 0xb9: "WEPKeyIndex", 0xba: "TIDALToken",
    0xbb: "SingleSpeaker", 0xbc: "ProductName", 0xbd: "FactoryCountry",
    0xbe: "GoogleCast", 0xbf: "ShareTimeout", 0xc0: "TimeZoneCast",
    0xc1: "CastTOS", 0xc2: "outputfs", 0xc3: "HardwarePlatform",
    0xc4: "Brand", 0xc5: "ProductType", 0xc6: "ProductReleaseTrack",
    0xc7: "ProductBuildType", 0xc8: "ProductBuildUser", 0xc9: "appsourcelist",
    0xca: "StereoPairMode", 0xcb: "StereoPairTimeOut", 0xcc: "CastSSIDSuffix",
}
VM_SSH = ["ssh", "-p", "2223", "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
          "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "root@localhost"]
VM_MTD = "/run/hwshim/mtd/mtd13"


def parse(image):
    """Return the items as a list of [id, flag, value-bytes]."""
    if image[:4] != MAGIC:
        raise SystemExit("not a CENV image (missing 'CENV' header)")
    items = []
    for off in range(PAGE, len(image), PAGE):
        page = image[off:off + PAGE]
        if page[:2] == b"\xff\xff":
            break
        items.append([page[0], page[1], page[2:].split(b"\0", 1)[0]])
    return items


def build(items):
    if (len(items) + 1) * PAGE > SIZE:
        raise SystemExit("too many items for the partition")
    image = bytearray(b"\xff" * SIZE)
    image[0:PAGE] = MAGIC.ljust(PAGE, b"\0")
    for n, (item_id, flag, value) in enumerate(items, 1):
        if len(value) + 3 > PAGE:
            raise SystemExit(f"value for {item_id:#x} is too long")
        image[n * PAGE:(n + 1) * PAGE] = (bytes([item_id, flag]) + value + b"\0").ljust(PAGE, b"\0")
    return bytes(image)


def parse_key(text):
    ident, _, flag = text.partition(":")
    by_name = {v.lower(): k for k, v in ITEM_NAMES.items()}
    key = ident.lower()
    ident = NAMES[key] if key in NAMES else by_name[key] if key in by_name else int(ident, 0)
    return ident, int(flag, 0) if flag else None


def find(items, key):
    item_id, flag = key
    return [i for i in items if i[0] == item_id and (flag is None or i[1] == flag)]


class Source:
    def __init__(self, args):
        self.args = args
        self.kind = "vm" if args.vm else "device" if args.device else "file"
        self.host = args.host

    def read(self):
        if self.kind == "file":
            with open(self.args.file, "rb") as f:
                return f.read()
        if self.kind == "vm":
            cmd = VM_SSH + [f"cat {VM_MTD}"]
        else:
            cmd = ["ssh", "-o", "LogLevel=ERROR", self.host, f"dd if=/dev/mtd/mtd13 bs={PAGE} count={SIZE // PAGE} 2>/dev/null"]
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
        if done.returncode:
            raise SystemExit(f"cannot read the {self.kind} (ssh exit {done.returncode}): {done.stderr.decode().strip()}")
        data = done.stdout
        if len(data) != SIZE:
            raise SystemExit(f"short read: {len(data)} bytes")
        return data

    def write(self, image):
        if self.args.write:
            with open(self.args.write, "wb") as f:
                f.write(image)
            print(f"wrote {self.args.write}")
        elif self.kind == "file":
            with open(self.args.file, "wb") as f:
                f.write(image)
        elif self.kind == "vm":
            subprocess.run(VM_SSH + [f"cat >{VM_MTD}"], input=image, check=True)
            print("written; reboot the VM so LibreEnv reloads it")
        else:
            if not self.args.yes:
                raise SystemExit("refusing to flash the unit without --yes (take a `dump` first)")
            tmp = "/tmp/cenv.new"
            subprocess.run(["ssh", "-o", "LogLevel=ERROR", self.host, f"cat >{tmp} && flash_image cenv {tmp}; rm -f {tmp}"],
                           input=image, check=True)
            print("flashed; reboot the unit now")


def label(item_id):
    return ITEM_NAMES.get(item_id, "")


def show_value(value):
    return value.decode("latin1")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m invoke_fw cenv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", help="CENV image file")
    ap.add_argument("--vm", action="store_true", help="use the simulator's mtd13")
    ap.add_argument("--device", action="store_true", help="use the real unit over ssh")
    ap.add_argument("--host", default="invoke", help="ssh host for --device")
    ap.add_argument("--write", metavar="FILE", help="write the result to FILE instead of back to the source")
    ap.add_argument("--yes", action="store_true", help="allow flashing the real unit")
    ap.add_argument("command", choices=["list", "get", "set", "rm", "dump"])
    ap.add_argument("rest", nargs="*")
    args = ap.parse_args(argv)
    if not (args.file or args.vm or args.device):
        ap.error("give -f FILE, --vm or --device")

    src = Source(args)
    image = src.read()

    if args.command == "dump":
        with open(args.rest[0], "wb") as f:
            f.write(image)
        print(f"saved {len(image)} bytes to {args.rest[0]}")
        return
    items = parse(image)

    if args.command == "list":
        for item_id, flag, value in items:
            print(f"{item_id:#04x}:{flag:#04x}  {label(item_id):<17} {show_value(value)!r}")
    elif args.command == "get":
        hits = find(items, parse_key(args.rest[0]))
        if not hits:
            raise SystemExit("not found")
        for item in hits:
            print(item[2].decode("latin1"))
    elif args.command == "set":
        key, value = parse_key(args.rest[0]), args.rest[1].encode()
        hits = find(items, key)
        if hits:
            for item in hits:
                item[2] = value
        else:
            items.append([key[0], key[1] if key[1] is not None else DEFAULT_FLAG, value])
        src.write(build(items))
    elif args.command == "rm":
        hits = find(items, parse_key(args.rest[0]))
        if not hits:
            raise SystemExit("not found")
        src.write(build([i for i in items if i not in hits]))
