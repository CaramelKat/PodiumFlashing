import os
import sys
import pexpect
import shutil
import argparse
import zlib
import traceback

from pathlib import Path
from enum import Enum
from processRunner import Command

EXTRACTION_PATH = './ExtractedImage'

BLOCK_SIZE = 131072

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

class Region():

    def __init__(self, name: str, bytes: bytes | None, bufferStart: int, imageSize: int, imagePath: Path, CRC32: bytes, unk2: bytes, partitionStart: int, partitionSize: int):
        self.name: str = name
        self.bytes = bytes
        self.imageSize: int = imageSize
        self.CRC32 = CRC32
        self.unk2 = unk2
        self.start: int = partitionStart
        self.partitionSize: int = partitionSize
        self.bufferStart: int = bufferStart

        self.path: Path = imagePath

    def __str__(self):
        return "| {:16s} | {:8s} {:11s} | {:8s} |  {:4s} | {:4s} {:11s} |".format(self.name,
            str(self.imageSize),
            '(' + str(round(self.imageSize * 0.00000095367432, 3)) + ' MB)',
            self.CRC32.hex(),
            str(self.start),
            str(self.partitionSize),
            '(' + str(round((self.partitionSize * BLOCK_SIZE) * 0.00000095367432, 3)) + ' MB)')

    def extractImage(self):
        filePath = Path(EXTRACTION_PATH, f'{self.name}.bin')
        with open(self.path, 'rb') as file:
            fileBytes = file.read()
            imageBuffer = fileBytes[self.bufferStart:self.bufferStart+self.imageSize]
            with open(filePath, 'wb') as w:
                w.write(imageBuffer)
                w.close()
            file.close()
            return True

    def updateImage(self):
        filePath = Path(EXTRACTION_PATH, f'{self.name}.bin')
        if not self.CRC32 or not self.bytes:
            return True
        print('Old image size:', self.imageSize)
        print(self.bytes.hex())
        
        with open(filePath, 'rb') as file:
            imageBuffer = file.read()
            newCRC32 = zlib.crc32(imageBuffer).to_bytes(4)
            fileSize = filePath.stat().st_size
            maxSize = self.partitionSize * BLOCK_SIZE
            if fileSize > maxSize:
                print(f'Error! Image size is too big! Must be at most {maxSize}. Is currently {fileSize}')
                file.close()
                return False

            self.imageSize = fileSize
            self.CRC32 = newCRC32
            self.actualLength = fileSize
            self.bytes = self.bytes[:16] + self.imageSize.to_bytes(4, 'little') + self.bytes[20:24] + newCRC32[::-1] + self.bytes[28:]
            print('New image size:', self.imageSize)
            print(self.bytes.hex())
            return True

class InvokeImage():

    def __init__(self, imagePath: Path):
        self.path: Path = imagePath
        self.header = None
        self.numRegions = -1
        self.regions: list[Region] = []

        Path(EXTRACTION_PATH).mkdir(parents=True, exist_ok=True) 
        self.initImage()

    def __str__(self):
        temp = []
        for region in self.regions:
            temp.append(str(region))
        return str({
            "path": self.path,
            "regions": temp
        })

    def printRegions(self):
        print('| Name             | Img Size             | CRC      | Start | Partition Size   |')
        print('| ---------------- + -------------------- + -------- + ----- + ---------------- |')
        for region in self.regions:
            print(region)
        print('| ---------------- | -------------------- | -------- | ----- | ---------------- |')

    def getRegion(self, name):
        return next((region for region in self.regions if region.name == name ), None)

    def initImage(self):
        fileBytes = b''

        with open(self.path, 'rb') as file:
            fileBytes = file.read()

        magicKey         = fileBytes[0:4]    # F1 A3 AD D2
        unk1             = fileBytes[0:2]    # 4A 08 ?
        unk2             = fileBytes[13]     # Unknown 1 byte - 0x08 / 8
        unk3             = fileBytes[20]     # Unknown 2 byte - 0x40 / 64
        unk4             = fileBytes[25]     # Unknown 3 byte - 0x08 / 8
        self.numRegions  = fileBytes[28]     # number regions - 0x09 / 9
        unk5             = fileBytes[32:35]  # Unknown 3 bytes - 23 41 30

        if magicKey != b'\xf1\xa3\xad\xd2':
            raise Exception("Magic key doesn't match! Unknown Image format!")

        self.header = fileBytes[0:64]

        offset = 64
        imageOffset = 640
        for i in range(0, self.numRegions):
            regionBuffer = fileBytes[offset:offset+64]
            # 8, 64, 8?
            # 8 bytes of padding before and after, each section is 64 bytes apart?
            sectionName = regionBuffer[0:16].decode("utf-8").rstrip('\x00') # Variable length, max 16 bytes
            imageSize = int.from_bytes(regionBuffer[16:20], 'little') # Actual image size
            CRC = regionBuffer[24:28][::-1] # CRC32
            unk2 = regionBuffer[32:36] # Always 0x1ef03301
            Start = int.from_bytes(regionBuffer[40:42], 'little') # Partition start? UBoot logs this, doesn't line up with the addresses so idk
            Size = int.from_bytes(regionBuffer[44:46], 'little') # Allocated partition size, not necessarily size of image

            region: Region = Region(name=sectionName, bytes=regionBuffer, bufferStart=imageOffset, imageSize=imageSize, imagePath=self.path, CRC32=CRC, unk2=unk2, partitionStart=Start, partitionSize=Size)
            self.regions.append(region)

            offset += 64
            imageOffset += imageSize

        # Manually add footer
        region: Region = Region(name='footer', bytes=None, bufferStart=imageOffset, imageSize=(len(fileBytes) - imageOffset), imagePath=self.path, CRC32=bytes(), unk2=bytes(), partitionStart=0, partitionSize=0)
        self.regions.append(region)

    def extractImage(self, name=None):
        if name:
            region = next((region for region in self.regions if region.name == name), None)
            if not region:
                return

            return region.extractImage()

        for region in self.regions:
            if not region.extractImage():
                print('Failed to extract', region.name, '!!!')
        # Copy image to extraction directory
        imagePath = Path(EXTRACTION_PATH, '83_IMAGE')
        shutil.copy(self.path, imagePath)
        return True

    def buildImage(self):
        # Start rebuilding the header
        fileBuffer = bytearray()
        imagesBuffer = bytearray()

        if not self.header:
            print(f'Error! Failed to build image due to missing header!')
            return False

        fileBuffer.extend(self.header)

        for region in self.regions:
            filePath = Path(EXTRACTION_PATH, f'{region.name}.bin')

            if not filePath.exists():
                print(f'Error! Failed to build image on region {region.name}!')
                return False

            with open(filePath, 'rb') as file:
                image = file.read()

                CRC32 = zlib.crc32(image).to_bytes(4)
                if CRC32 != region.CRC32 and region.CRC32 != b'':
                    print(f'Updating {region.name} new: {CRC32.hex()} old: {region.CRC32.hex()}')
                    region.updateImage()

                if region.bytes:
                    fileBuffer.extend(region.bytes)

                if region.name != 'header':
                    imagesBuffer.extend(image)

                file.close()

        fileBuffer.extend(imagesBuffer)
        newImagePath = Path(EXTRACTION_PATH, '83_IMAGE_REBUILT')
        with open(newImagePath, 'wb') as w:
            w.write(fileBuffer)
            w.close()
            return True

def extractImage(image: InvokeImage):
    print(f'Extracting image to {EXTRACTION_PATH}')
    if image.extractImage():
        print('Parsed regions:')
        image.printRegions()
        print('Extraction complete!')
    else:
        print('Failed to extract image!')
        exit(1)

def buildImage(image: InvokeImage):
    if not any(Path(EXTRACTION_PATH).iterdir()):
        print('Image has not been extracted yet!')
        exit(1)

    print('Building image...')
    if image.buildImage():
        print('Generated regions:')
        image.printRegions()
        print('Build complete!')
        return True
    else:
        print('Failed to build image!')
        exit(2)

def flashImage(imagePath: Path, verbose: bool):
    if not imagePath:
        imagePath =  Path(EXTRACTION_PATH, '83_IMAGE_REBUILT')

    if not imagePath.exists():
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
    newImagePath = (flashDIR / '83_IMAGE')
    newImagePath.unlink(missing_ok=True)
    shutil.copy(imagePath, newImagePath)

    usbBootBin = flashDIR / Path('usb_boot')
    process = Command([usbBootBin, '1286', '8174', f'{flashDIR.resolve()}/', '8141'], verbose)

    process.start()
    client = None
    try:
        process.waitForLine('tcp_server_func, 869: server listening on port')
        print('Telnet server started')

        client = pexpect.spawn("telnet 127.0.0.1 8141")
        if verbose:
            client.logfile = sys.stdout.buffer

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

        client.expect('do_usbload, loading image 83', timeout=120)
        print('Sending 83_IMAGE to Invoke...')
        process.log = True

        client.expect('do_l2nand, loading image 0x83', timeout=30)
        process.log = False
        print('Invoke loading image...')

        client.expect('Erase NAND chip...', timeout=120)
        print('Erasing NAND...')
        client.logfile = sys.stdout.buffer

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

    if client:
        client.close()
    process.stop()

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

    image = InvokeImage(args.path) if args.path else None

    match args.command:
        case Commands.extract:
            if not image:
                print('Error! Image does not exist')
                return
            extractImage(image)

        case Commands.build:
            if not image:
                print('Error! Image does not exist')
                return
            buildImage(image)

        case Commands.clean:
            shutil.rmtree(EXTRACTION_PATH)
            print('Cleaned image directory')

        case Commands.unpackFS:
            unpackFS()

        case Commands.packFS:
            packFS()

        case Commands.flash:
            flashImage(args.path, args.verbose)

        case Commands.quickBuild:
            if not image:
                print('Error! Image does not exist')
                return
            packFS()
            buildImage(image)
            flashImage(Path(EXTRACTION_PATH, '83_IMAGE_REBUILT'), args.verbose)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print(bcolors.FAIL + 'Error! This script must be run as root' + bcolors.ENDC)
        exit(-1)

    parser = argparse.ArgumentParser("marvell_imager")

    parser.add_argument('command', type=Commands, choices=list(Commands), help="Action to perform on image",)
    parser.add_argument("path", nargs="?", help="Path to image to extract", type=Path)
    parser.add_argument('-v', '--verbose', action='store_true')

    args = parser.parse_args()

    main(args)