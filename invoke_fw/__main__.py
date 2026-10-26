"""Command line for the Invoke firmware library.  Run from the repo root:  python3 -m invoke_fw <command> ...

  info IMAGE                         list the regions of an 83_IMAGE and check their CRCs
  extract IMAGE DIR [-r REGION]      write the regions to DIR/<region>.bin (only REGION with -r)
  build IMAGE DIR OUT                rebuild IMAGE with every DIR/<region>.bin and write OUT
  replace IMAGE REGION FILE OUT      swap one region for FILE (size and CRC fixed) and write OUT
  rootfs-extract IMAGE OUT           write the rootfs squashfs of IMAGE to OUT
  rootfs-replace IMAGE FILE OUT      same as `replace IMAGE rootfs FILE OUT`, printing the spare room
  check                              report which external tools (squashfs-tools, ssh, docker) are installed
  squashfs FILE                      print a squashfs superblock
  fenv ...                           read/edit FENV.bin   (python3 -m invoke_fw fenv --help)
  cenv ...                           read/edit the CENV store   (python3 -m invoke_fw cenv --help)
"""
import sys

from . import cenv, fenv, rootfs
from .image import ImageError, load_image


def checked(path):
    image = load_image(path)
    problems = image.verify()
    if problems:
        sys.exit(f'{path}: ' + '; '.join(problems))
    return image


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        sys.exit(__doc__)
    command, args = argv[0], argv[1:]
    if command == 'fenv':
        return fenv.main(args)
    if command == 'cenv':
        return cenv.main(args)
    try:
        if command == 'info' and len(args) == 1:
            image = load_image(args[0])
            print(image.table())
            problems = image.verify()
            print('OK' if not problems else 'PROBLEMS: ' + '; '.join(problems))
            return 1 if problems else 0
        if command == 'extract' and len(args) in (2, 4) and (len(args) == 2 or args[2] == '-r'):
            checked(args[0]).extract_to(args[1], args[3] if len(args) == 4 else None)
        elif command == 'build' and len(args) == 3:
            image = load_image(args[0])
            print('changed:', ', '.join(image.load_dir(args[1])) or 'nothing')
            image.save(args[2])
        elif command == 'replace' and len(args) == 4:
            image = checked(args[0])
            with open(args[2], 'rb') as f:
                image.replace(args[1], f.read())
            image.save(args[3])
        elif command == 'rootfs-extract' and len(args) == 2:
            with open(args[1], 'wb') as f:
                f.write(checked(args[0]).get('rootfs').data)
        elif command == 'rootfs-replace' and len(args) == 3:
            image = checked(args[0])
            with open(args[1], 'rb') as f:
                new = f.read()
            old = image.get('rootfs').size
            image.replace('rootfs', new)
            image.save(args[2])
            print(f'rootfs {old} -> {len(new)} bytes ({image.get("rootfs").capacity - len(new)} spare in the partition)')
        elif command == 'check' and not args:
            missing = False
            for name, path, purpose, required in rootfs.check_tools():
                print(f'{"ok" if path else "MISSING":8}{name:12}{path or ""}  [{purpose}]')
                missing |= required and not path
            if missing:
                sys.exit('install squashfs-tools (macOS: brew install squashfs)')
        elif command == 'squashfs' and len(args) == 1:
            with open(args[0], 'rb') as f:
                for key, value in rootfs.superblock(f.read(96)).items():
                    print(f'{key:22} {value}')
        else:
            sys.exit(__doc__)
    except (ImageError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
