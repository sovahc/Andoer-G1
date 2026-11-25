#!/usr/bin/env python3
"""Build firmware/FREAL1A.bin (the card update file) from the NOR dump.

The LD004 loader (NOR 0x0-0x3fff, stage 2 = BCL1 block at NOR 0x92c) takes FREAL1A.BIN from the
card root at every boot: the file must be exactly the BCL1 block (16-byte header + compressed
stream, length = BE32 at +0xc plus 16), pass  sum(i + halfword[i]) == 0 (mod 2^16)  over its
halfwords, and is then erased+programmed at NOR 0x4000 and verified by readback ("FW check fail",
"verify fail" = print + hang). The loader never reads header bytes +4..+7.

  ./g1_mkfw.py [nor_dump] [outfile]        # defaults: ../firmware/g1_spi_1.bin, ../firmware/FREAL1A.bin

Patch (task.md, 23.11 session 3): the USBDISK mode struct 0x8051dc50 {name, +4 run at mode open,
+8 run at mode close} gets the USBCAM functions, so the boot flow "USB inserted -> post USBDISK"
opens the webcam. Four literal bytes of the LZ stream, decompressed 0x51dc58 (+4): 0x8022a158 ->
0x8022972c, 0x51dc5c (+8): 0x8022a11c -> 0x802296e8. One later match of the stream copies the
patched "a1 22 80", so a fifth decompressed byte changes with them: 0x51dcad a1 -> 96 = the MSDC
window's 0x04000005 (stop) handler 0x8022a1e8 -> 0x802296e8 (forwards the stop to the UVC window).
That window object (0x8051dcb8) is addressed only by the two USBDISK functions the patch retires,
so the byte is unreachable once both pointers are swapped; it is asserted here, not ignored.

Cardless boot (24.11 session 10): the boot flow above posts USBDISK only with a card inserted --
System_OnUsbInsert 0x801f1a54 reads SysConfig[0xab] (card present) at 0x801f1adc and skips the post
("CARD_REMOVED, skip mass storage mode!"). Two single literals redirect type 1 ("CONNECT to PC") around
that check:
  NOR 0x11e03b  DxUSB_UpdateConnectType 0x801e37c8  beql v0,zero,+0x0a -> +0x10: raw "PC" returns
               connect type 3 ("Charging PC") instead of 1, without its printf. Every caller of the
               type getter 0x802fd09c (14 sites) tests {1,3} alike or only 2/-1/nonzero; the change hook
               0x805b0304 is never registered (setter 0x802fd07c has no caller).
  NOR 0x125813  System_OnUsbInsert 0x801f1aa0       beq v0,v1(3),+0x0e -> +0x11: type 3 jumps to 0x801f1ae8
               (the delay slot that loads v0 = 0x80520000) instead of the card check at 0x801f1adc.
Type 2 (charger) is untouched. Both literals have no other copy in the stream; the alternative of
retargeting the beqz itself (offset byte 0x1d at NOR 0x124d36, -> 0x01) was rejected: that literal is
shared with PhotoTimerCB's "motion capture done" branch 0x801f0598, and every other byte of the beqz
is shared by 15..56 other instructions.
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'firmware', 'g1_spi_1.bin')
DST = os.path.join(HERE, '..', 'firmware', 'FREAL1A.bin')
LZC = os.path.join(HERE, 'ntk_lz_dec.c')

# NOR offset, old bytes, new bytes; all literals of the stream (tools/g1_lztrack.py at <offset>)
PATCH = ((0x2e915e, '58a1', '2c97'), (0x2e9162, '1ca1', 'e896'),
         (0x11e03b, '0a', '10'), (0x125813, '0e', '11'))
DEC_OLD, DEC_NEW = (0x51dc58, '58a12280', '2c972280'), (0x51dc5c, '1ca12280', 'e8962280')
COLLATERAL = (0x51dcad, 0xa1, 0x96)
# decompressed offset, factory byte, patched byte
CARDLESS = ((0x1e37c8, 0x0a, 0x10), (0x1f1aa0, 0x0e, 0x11))


def weighted_sum(b):
    n = len(b) // 2
    return sum((i + h) & 0xffff for i, h in enumerate(struct.unpack('<%dH' % n, b[:2 * n]))) & 0xffff


def decompress(block):
    exe = tempfile.mktemp(prefix='lz')
    subprocess.run(['gcc', '-O2', '-o', exe, LZC], check=True)
    src, out = tempfile.mktemp(prefix='bcl'), tempfile.mktemp(prefix='dec')
    open(src, 'wb').write(block)
    subprocess.run([exe, src, '0x10', '0x%x' % (len(block) - 0x10), '0x800000', out], check=True,
                   stderr=subprocess.DEVNULL)
    return open(out, 'rb').read()


def build(src, dst):
    nor = bytearray(open(src, 'rb').read())
    magic, comp = nor[0x4000:0x4004], struct.unpack_from('>I', nor, 0x400c)[0]
    assert magic == b'BCL1' and comp == 0x2f6bd4, (magic, comp)
    block = bytearray(nor[0x4000:0x4000 + comp + 0x10])
    assert weighted_sum(block) == 0, 'factory block checksum is not zero'

    before = weighted_sum(block)
    for off, old, new in PATCH:
        old, new = bytes.fromhex(old), bytes.fromhex(new)
        assert nor[off:off + len(old)] == old, '%x: %s' % (off, nor[off:off + len(old)].hex())
        block[off - 0x4000:off - 0x4000 + len(new)] = new
    fix = (before - weighted_sum(block)) & 0xffff
    struct.pack_into('<H', block, 4, (struct.unpack_from('<H', block, 4)[0] + fix) & 0xffff)
    assert weighted_sum(block) == 0

    ref, new = decompress(bytearray(nor[0x4000:0x4000 + comp + 0x10])), decompress(block)
    diff = [i for i, (a, b) in enumerate(zip(ref, new)) if a != b]
    expected = [DEC_OLD[0], DEC_OLD[0] + 1, DEC_NEW[0], DEC_NEW[0] + 1, COLLATERAL[0]] + [c[0] for c in CARDLESS]
    assert len(ref) == len(new) and ref[DEC_OLD[0]:DEC_OLD[0] + 4].hex() == DEC_OLD[1] and \
        ref[DEC_NEW[0]:DEC_NEW[0] + 4].hex() == DEC_NEW[1] and \
        new[DEC_OLD[0]:DEC_OLD[0] + 4].hex() == DEC_OLD[2] and new[DEC_NEW[0]:DEC_NEW[0] + 4].hex() == DEC_NEW[2] and \
        (ref[COLLATERAL[0]], new[COLLATERAL[0]]) == (COLLATERAL[1], COLLATERAL[2]) and \
        all((ref[o], new[o]) == (a, b) for o, a, b in CARDLESS) and sorted(diff) == sorted(expected), diff
    open(dst, 'wb').write(block)
    print('%s: 0x%x bytes, checksum fix +0x%04x in header +4, decompressed image differs at %s'
          % (dst, len(block), fix, ' '.join('0x%x' % i for i in diff)))


if __name__ == '__main__':
    build(sys.argv[1] if len(sys.argv) > 1 else SRC, sys.argv[2] if len(sys.argv) > 2 else DST)
