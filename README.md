# Harman-Kardon Invoke Firmware Modification Tool

This repo allows you to extract, modify, and rebuild an 83_IMAGE to flash to the HK Invoke.

A lot of knowledge came from: [HKHacking](https://github.com/coggy9/HKHacking), so thank you to [coggy9](https://github.com/coggy9), [Ryan](https://github.com/rbeesley), and others who have contributed to the research on how the Invoke works!

The marvell_flash_tool comes from the [Harman.Kardon.INVOKE.Flashing.zip](https://github.com/coggy9/HKHacking/releases/tag/HarmanFlash) tool provided by Harman-Kardon support to flash the last OTA update

## Flashing from the browser

Open the hosted page (<https://caramelkat.github.io/PodiumFlashing/>) in Chrome or Edge, pick an image, and follow the on-page steps. It uses WebUSB, so no tools need to be installed. The page is `webusb/index.html`; hosted images live in `firmware/` (Git LFS, see `firmware/README.md`).

To run it locally: `python3 webusb/devserver.py`, then open <http://localhost:8000/>.
