import os
import sys
import time
import threading
import atexit
import pexpect
import shutil
import argparse
import traceback

from pathlib import Path
from enum import Enum
from processRunner import Command
from invoke_fw import BLOCK_SIZE, ImageError, load_image

EXTRACTION_PATH = './ExtractedImage'

SKIP_SCRIPT = b'#skip\n'

REQUIRED_FILES = [
    Path('06_IMAGE'),
    Path('07_IMAGE'),
    Path('09_IMAGE'),
    Path('79_IMAGE'),
    Path('83_IMAGE'),
    Path('bcm_erom.bin.usb'),
    Path('bootloader.img'),
    Path('drm_erom.img'),
    Path('sysinit.img'),
    Path('usb_boot')
]

class TaggedLog:
    """Interleaves usb_boot and telnet output as timestamped, source-tagged lines."""
    SOURCES = {
        'usb_boot': '\033[93m',
        'telnet':   '\033[96m',
        'imager':   '\033[91m',
    }

    def __init__(self, logPath=None):
        self.file = open(logPath, 'w', buffering=1) if logPath else None
        self.lock = threading.Lock()
        self.start = time.monotonic()
        self.partial = {}

    def line(self, source, text):
        stamp = time.monotonic() - self.start
        color = self.SOURCES[source]
        with self.lock:
            sys.stdout.write(f'{color}[{stamp:8.3f}] {source:<8} | {text}\033[0m\n')
            sys.stdout.flush()
            if self.file:
                self.file.write(f'[{stamp:8.3f}] {source:<8} | {text}\n')

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

    def printer(self, source):
        return lambda text: self.line(source, text)

    def writer(self, source):
        # File-like object for pexpect's logfile; buffers until a full line arrives.
        log = self
        class Writer:
            def write(self, data):
                text = log.partial.get(source, '') + data.decode(errors='replace').replace('\r', '')
                *lines, log.partial[source] = text.split('\n')
                for l in lines:
                    if l:
                        log.line(source, l)
            def flush(self):
                pass
        return Writer()

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

class Commands(Enum):
    extract = 'extract'
    unpackFS = 'unpack-fs'
    packFS = 'pack-fs'
    build = 'build'
    clean = 'clean'
    flash = 'flash'
    quickBuild = 'q-build'

    def __str__(self):
        return self.value

def extractImage(path: Path):
    print(f'Extracting image to {EXTRACTION_PATH}')
    image = load_image(path)
    image.extract_to(EXTRACTION_PATH)
    # The original image is kept as the template that build uses for the header and region table
    shutil.copy(path, Path(EXTRACTION_PATH, '83_IMAGE'))
    print('Parsed regions:')
    print(image.table())
    print('Extraction complete!')

def buildImage(path: Path):
    if not Path(EXTRACTION_PATH).exists() or not any(Path(EXTRACTION_PATH).iterdir()):
        print('Image has not been extracted yet!')
        exit(1)

    print('Building image...')
    try:
        image = load_image(path)
        for name in image.load_dir(EXTRACTION_PATH):
            print(f'Updating {name}')
        image.save(Path(EXTRACTION_PATH, '83_IMAGE_REBUILT'))
    except ImageError as error:
        print(f'Failed to build image! {error}')
        exit(2)
    print('Generated regions:')
    print(image.table())
    print('Build complete!')

def uboot_input_filter():
    # U-Boot doesn't echo over this link and the pty echo is disabled, so echo locally and map DEL to ^H.
    lineLen = 0

    def filt(data: bytes) -> bytes:
        nonlocal lineLen
        out = bytearray()
        for b in data:
            if b in (0x7f, 0x08):
                if lineLen:
                    lineLen -= 1
                    sys.stdout.write('\b \b')
                out.append(0x08)
            elif b in (0x0d, 0x0a):
                lineLen = 0
                out.append(b)
            elif 0x20 <= b < 0x7f:
                lineLen += 1
                sys.stdout.write(chr(b))
                out.append(b)
            else:
                out.append(b)
        sys.stdout.flush()
        return bytes(out)

    return filt

def flashImage(imagePath: Path, verbose: bool, logFile: Path = None, ubootShell: bool = False):
    if not imagePath and not ubootShell:
        imagePath =  Path(EXTRACTION_PATH, '83_IMAGE_REBUILT')

    if not ubootShell and not imagePath.exists():
        print(f'Error! {args.path} was not found!!')
        exit(4)

    print('Getting ready to flash image...')

    flashDIR = Path('./marvell_flash_tool')
    if not flashDIR.is_dir():
        print(f'Error! Missing {flashDIR}. Flashing cannot continue!')
        exit(4)

    # Check that we have all the required files
    for file in REQUIRED_FILES:
        if not (flashDIR / file).exists():
            print(f'Error! Missing {file} from {flashDIR}. Flashing cannot continue!')
            exit(3)

    # Copy image to flashing directory
    if not ubootShell:
        newImagePath = (flashDIR / '83_IMAGE')
        newImagePath.unlink(missing_ok=True)
        shutil.copy(imagePath, newImagePath)

    if ubootShell:
        # A 79_IMAGE containing flash commands would start flashing; swap in a script that stops at the prompt.
        bootScript = flashDIR / '79_IMAGE'
        originalScript = bootScript.read_bytes()
        atexit.register(bootScript.write_bytes, originalScript)
        bootScript.write_bytes(SKIP_SCRIPT)

    usbBootBin = flashDIR / Path('usb_boot')
    tagged = TaggedLog(logFile) if verbose else None
    if tagged and logFile:
        print(f'Writing verbose log to {logFile}')
    process = Command([usbBootBin, '1286', '8174', f'{flashDIR.resolve()}/', '8141'], verbose,
                      tagged.printer('usb_boot') if verbose else None)

    process.start()
    client = None
    try:
        process.waitForLine('tcp_server_func, 869: server listening on port')
        print('Telnet server started')

        client = pexpect.spawn("telnet 127.0.0.1 8141")
        if verbose:
            client.logfile = tagged.writer('telnet')

        print(bcolors.HEADER + '\n============================ Invoke Flasher============================' + bcolors.ENDC)
        print('Follow the directions below to flash your Invoke:')
        print('1. Connect the Invoke to your computer via USB')
        print('2. Press and hold the reset button on the Invoke')
        print('3. Connect power to the Invoke')
        print('4. Press the mute button 4 times')
        print('5. Continue holding the reset button until you see "Device USB booting!')
        print(bcolors.HEADER + '=======================================================================' + bcolors.ENDC)

        client.expect('one target device connected.', timeout=300)
        print('\nDevice connected! Keep holding reset!')

        client.expect('!!!USB_Boot!!!', timeout=120)
        print('\nDevice USB booting! Stop holding reset now.')

        client.expect('Marvell U-boot', timeout=120)
        print('U-Boot Shell Loaded')

        if ubootShell:
            client.expect(r'MV88DE3100\|>', timeout=60)
            client.logfile = None
            process.log = False
            print(bcolors.OKGREEN + '\nInteractive U-Boot shell. Press Ctrl+] to exit.' + bcolors.ENDC)
            client.setecho(False)
            client.send('\r')
            client.interact(input_filter=uboot_input_filter())
            client.close()
            process.stop()
            return

        client.expect('do_usbload, loading image 83', timeout=120)
        print('Sending 83_IMAGE to Invoke...')
        process.log = True

        client.expect(r'do_l2nand, load(ing|img) image 0x83', timeout=30)
        process.log = verbose
        print('Invoke loading image...')

        client.expect('Erase NAND chip...', timeout=120)
        print('Erasing NAND...')
        client.logfile = tagged.writer('telnet') if verbose else sys.stdout.buffer

        client.expect('Congratulations! u2nand succeed!', timeout=300)

        client.logfile = None
        print(bcolors.OKGREEN + '\nFlashing device is complete! You can now reboot your invoke' + bcolors.ENDC)
        client.close()
        process.stop()
    except KeyboardInterrupt:
        if client:
            client.close()
        process.stop()
        print(bcolors.FAIL + 'Error! User interrupted flashing!' + bcolors.ENDC)
        exit(1)
    except Exception:
        if client:
            client.close()
        process.stop()
        print(bcolors.FAIL + 'Error! Failed to flash device!' + bcolors.ENDC)
        print('Make sure you are following the instructions above!')
        print('Make sure you have the correct image selected!')
        if verbose:
            print(traceback.format_exc())
            for line in traceback.format_exc().splitlines():
                tagged.line('imager', line)

    if client:
        client.close()
    process.stop()
    if tagged:
        tagged.close()

def unpackFS():
    # Check if rootfs.bin exists
    rootfsPath = Path(EXTRACTION_PATH, 'rootfs.bin')
    if not rootfsPath.exists():
        print('Error! rootfs.bin not found!')
        exit(1)

    # Remove old rootfs directory
    rootfsDir = Path(EXTRACTION_PATH, 'rootfs')
    if rootfsDir.exists():
        shutil.rmtree(rootfsDir)
        print('Removed old rootfs directory')

    # Use unsquashfs to unpack the filesystem
    print('Unpacking rootfs.bin...')
    unsquashfsCommand = Command(['unsquashfs', '-f', '-d', rootfsDir, str(rootfsPath)], True)
    unsquashfsCommand.start()

    print('Unpacking complete!')
    print('Unpacked filesystem to', EXTRACTION_PATH)

def packFS():
    # Check if rootfs exists
    rootfsPath = Path(EXTRACTION_PATH, 'rootfs')
    if not rootfsPath.exists():
        print('Error! rootfs directory not found!')
        exit(1)

    # Use mksquashfs to pack the filesystem
    print('Packing rootfs...')
    mksquashfsCommand = Command(['mksquashfs', str(rootfsPath), str(Path(EXTRACTION_PATH, 'rootfs.bin')), '-b', BLOCK_SIZE, '-comp', 'gzip', '-noappend'], True)
    mksquashfsCommand.start()
    mksquashfsCommand.wait()

    print('Packing complete!')

def main(args):
    if args.command in [Commands.build, Commands.quickBuild]:
        args.path = Path(EXTRACTION_PATH, '83_IMAGE')

    if args.path and args.command in [Commands.extract, Commands.build, Commands.flash] and not Path(args.path).exists():
        print(f'Error! {args.path} was not found!')
        exit(4)

    match args.command:
        case Commands.extract:
            if not args.path:
                print('Error! Image does not exist')
                return
            extractImage(args.path)

        case Commands.build:
            buildImage(args.path)

        case Commands.clean:
            shutil.rmtree(EXTRACTION_PATH)
            print('Cleaned image directory')

        case Commands.unpackFS:
            unpackFS()

        case Commands.packFS:
            packFS()

        case Commands.flash:
            flashImage(args.path, args.verbose, args.log_file, args.uboot_shell)

        case Commands.quickBuild:
            packFS()
            buildImage(args.path)
            flashImage(Path(EXTRACTION_PATH, '83_IMAGE_REBUILT'), args.verbose, args.log_file)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print(bcolors.FAIL + 'Error! This script must be run as root' + bcolors.ENDC)
        exit(-1)

    parser = argparse.ArgumentParser("marvell_imager")

    parser.add_argument('command', type=Commands, choices=list(Commands), help="Action to perform on image",)
    parser.add_argument("path", nargs="?", help="Path to image to extract", type=Path)
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--log-file', type=Path, default=Path('flash.log'),
                        help='Where verbose flashing output is saved (default: flash.log)')

    parser.add_argument('--uboot-shell', action='store_true',
                        help='With flash: boot to the U-Boot prompt and attach an interactive shell instead of flashing')

    args = parser.parse_args()

    main(args)