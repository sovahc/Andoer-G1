# Andoer G1 (Novatek NT96658) — firmware that boots as a USB webcam

A cheap clip-on action camera ("Andoer G1", 4 MB SPI NOR, Novatek NT96658). Its stock firmware
contains a complete UVC webcam (VideoControl + VideoStreaming + AudioControl, MJPG and H.264 up to
1920x1080@30), but nothing in the boot flow can select it: on plug-in the camera always becomes USB
mass storage. `v2-webcam/` is the stock image with an 8-byte patch so it comes up as a UVC webcam
(`0603:8612`) on every plug-in, on any PC, with no card, no driver and no host software.
`stock/` is the unmodified factory image, for going back.

Flashing is done by the camera's own factory loader from a microSD card — no programmer, no soldering,
no other tools.

## Flash it

1. Put the card in a **card reader** and delete `FREAL1A.BIN` from its root if it is there. While the
   file sits on the card, the loader re-flashes the camera at **every** power-on.
2. Copy the wanted `FREAL1A.BIN` (from `v2-webcam/` or from `stock/`) to the **root** of the card and
   keep the name exactly `FREAL1A.BIN`. Eject properly.
3. Put the card into the camera, power it from USB.
4. LEDs during the update: **slow red blinking → fast red blinking → solid red**. Solid red means the
   flash is finished and the camera just sits there — it does not reboot itself.
5. Power-cycle it (unplug / replug USB).
   * `v2-webcam` → **blue** LED, UVC camera on the bus, no card needed.
   * `stock` with the card in → mass storage (`0603:8611`); `stock` without a card → yellow LED, no USB.
6. Delete `FREAL1A.BIN` from the card again. The camera does not delete it for you — we never once saw it
   disappear by itself — and in webcam mode the camera does not expose the card at all, so the host cannot
   reach the file: use the card reader, or simply do not put that card into the camera again.

The card has to be FAT32 (the camera's own format is fine). Nothing else on the card matters.

## Without touching the flash (stock firmware only)

Stock firmware exposes a factory console on the mass-storage interface, and one line is enough to get
the webcam until the next power cycle:

```
sudo python3 tools/nvtcall.py /dev/sg1 line mode pcc
```

`/dev/sg1` = the camera (check `cat /sys/class/scsi_device/*/device/model`, it is `"Life Cam"`;
`modprobe sg` if there is no `/dev/sg*`). This needs the stock image — with `v2-webcam` the disk
interface never comes up. `tools/nvtcall.py` can also read and write device RAM and dump the console
dictionary; see its docstring.

## Files

| path | what | sha256 |
|---|---|---|
| `v2-webcam/FREAL1A.BIN` | webcam firmware, 3 107 812 B | `55fb526a27686aa1121db3ed1d4f0671b6ba646928e30d3cb7d2c49c71a4b619` |
| `stock/FREAL1A.BIN` | factory firmware, 3 107 812 B | `bc6e18c391da415f45a3e6cc66395d526b22494ef30966717044dca6b25b02bd` |
| `tools/nvtcall.py`, `tools/nvtscsi.py` | console + RAM access over USB mass storage (stock only) | |
| `tools/g1_mkfw.py` | builds the webcam update file from a 4 MB NOR dump and checks the result (`python3 tools/g1_mkfw.py <dump.bin> FREAL1A.BIN`) | |
| `tools/bfc4ntk`, `tools/ntk_lz_dec.c` | pack/unpack the `BCL1` block; the LZ77 stream decoder | |

Verified on hardware: the webcam image was flashed from a card on a stock camera and both directions
(stock → webcam, webcam → stock) were repeated several times.

## Risk

Everything here writes the SPI NOR only through the camera's own loader, and the card route writes NOR
only from `0x4000` upward (unless you deliberately put `LREAL1A.BIN` on the card, which replaces the
loader), so a bad image can always be replaced by flashing `stock/FREAL1A.BIN` the same way. A power cut
*during* the write leaves a camera that boots into nothing and needs an SPI programmer and a working
image (a full 4 MB dump: `flashrom -p serprog:dev=/dev/ttyACM0:115200,spispeed=2M -c MX25L3233F/MX25L3273E -w dump.bin`).
Before flashing, it is worth dumping your own camera's NOR once and keeping the file: the serial number
and WiFi identity live in NOR at `0x3A2000` and are not part of the update file.

## Notes for anyone digging further

Everything below was verified on this camera; it is the short version, the long reverse-engineering
log is not published because most of it can be re-derived from the image.

* SoC NT96658MBG, MIPS32 little-endian, eCos + µITRON. In the decompressed image,
  `VA = 0x80000000 + file offset`.
* NOR 4 MB (`MX25L3233F`): `0x0–0x3FFF` factory loader ("LD004", stage 1 at `0x280`, linked at
  `0xA7F00000`, which unpacks a nested `BCL1` block at `0x92C` to `0x87F05000` and runs it);
  `0x4000` main image = 16-byte `BCL1` header + LZ77 stream (0x2f6bd4 → 0x53bed0 bytes);
  `0x380200`/`0x381200` = `CUSINSTALL`/`SYSP` parameter store; `0x3A2000` ManufacturingInfo
  (model, serial, WiFi SSID/pass, MAC — no VID/PID, those come from a config XML).
* The loader mounts the card with its own FAT driver at **every** boot and looks in the root for
  `FREAL1A.BIN` (write NOR from `0x4000`), `LREAL1A.BIN` (write the loader itself to NOR `0x0`) and
  `FREAL1T.BIN` (boot from RAM, no write) — of these three only `FREAL1A.BIN` is tested on hardware, the
  other two are read out of the loader code. Acceptance test: `'BCL1'` at offset 0, file length
  `== BE32(+0xc) + 0x10`, and `sum(i + LE16 halfword[i]) == 0 (mod 2^16)` over the whole file, where
  the checksum field itself is the LE16 at header `+4`. Header `+4..+7` are otherwise unused.
  A rejected file prints to a UART at `0xB0290000` that is not wired out and hangs in a loop; from
  outside that looks like a dead camera, so check the file before blaming the hardware.
* LZ77 stream: the escape marker is the first byte of the stream; a byte `!= marker` is a literal,
  `marker 00` is a literal marker, otherwise `marker varint(len) varint(off)` copies `len` bytes from
  distance `off` of the output (overlap allowed). Varints are big-endian, 7 bits per byte, `0x80` = continue.
* The webcam patch is **data, not code**. App modes are structs `{name, fn_open, fn_close}` in `.data`;
  USBDISK's struct at `0x8051dc50` gets USBCAM's functions (`open 0x8022972c`, `close 0x802296e8`), so
  the normal boot flow ("USB inserted → post USBDISK") opens the UVC class instead of the disk.
* Boot flow posts USBDISK only when a card is present (`System_OnUsbInsert 0x801f1a54` reads
  `SysConfig[0xab]` at `0x801f1adc`). The cardless variant redirects two branch offsets so the
  "CONNECT to PC" connect type skips that check. The type-2 (charger) branch is untouched.
* Byte-level diff against factory, in NOR addresses (card-file offset `+ 0x4000`), measured between the
  two images in this repository:
  * webcam with card — 6 bytes: `0x4004` `68→C8`, `0x4005` `09→14` (checksum `0x0968 → 0x1DC8`),
    `0x2E915E` `58→2C`, `0x2E915F` `A1→97`, `0x2E9162` `1C→E8`, `0x2E9163` `A1→96`;
  * webcam without card (`v2-webcam/`) — the same four pointer bytes, checksum `0x0968 → 0x14C8`, and
    `0x11E03B` `0A→10`, `0x125813` `0E→11`: 8 bytes in total.
  All of them are exclusive literals of the LZ stream, so nothing else in the stream moves: same length,
  same header sizes, no re-compression, `0xDB` (the escape marker) never appears. Rebuilding from your
  own dump is not possible by patching those bytes unless your dump is identical to this camera's;
  use `tools/g1_mkfw.py` which patches, repacks and re-checks the decoded image.
* The boot mode decision consults **nothing** configurable: no `SysConfig` key, no PStore value and no
  config XML key can select the webcam — that audit is exactly why a patch was needed. The card files the
  firmware looks at around boot are `A:\KEEPFW` (leave the update file in place) and `A:\engmode`
  (service/calibration mode); neither of them selects the webcam.
* Console over USB (stock only): SCSI vendor commands on the mass-storage LUN — opcode `0xC5` command,
  `0xC3` copy device RAM → host, `0xC4` host → device RAM; `CDB[1] == 7`, `CDB[2..5]` BE32 device
  address, `CDB[6..9]` BE32 length (`<= 0x10000`), `CDB[10]` group (`0` DbgSys, `1` BiCall, `2` SiCall,
  `0xff` Misc), `CDB[11]` function; commands run in a poll task, so a mode switch from the host does
  not deadlock the USB task.
* Known limitations of the webcam mode, both on stock and on the patched image: the microphone is
  described (UAC1, isoc EP `0x83`) but never fed, `arecord` gets `EIO` — nothing on the host fixes it;
  and the kernel logs `UVC non compliance: Reducing max payload transfer size (2097154) to fit endpoint
  limit (512)` on every stream start, which is harmless. The default format after boot is H.264
  1920x1080; apps that only speak MJPEG need `-input_format mjpeg` (or a v4l2loopback re-export).
