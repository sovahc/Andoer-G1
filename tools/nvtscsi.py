#!/usr/bin/env python3
"""SCSI pass-through (SG_IO) for the Novatek "NvUSB" channel over USB mass storage.

Needs root; works on a block device (/dev/sdc) as well as on /dev/sgN.

  nvtscsi.py <dev> test
  nvtscsi.py <dev> cdb <hex bytes...> [-i N | -o hexbytes] [-t ms] [-v]

  -i N   read N bytes from the device (DATA IN,  bmCBWFlags bit7 = 1)
  -o HH  write these bytes to the device (DATA OUT)
"""
import ctypes
import fcntl
import os
import sys

# ioctl 0x2285 (what sg3_utils uses) takes the *v3* sg_io_hdr; handing it the newer
# sg_io_v4 struct is answered with EMSGSIZE.
SG_IO = 0x2285
DIR_NONE, DIR_TO_DEV, DIR_FROM_DEV = -1, -2, -3


class Req(ctypes.Structure):
    _fields_ = [
        ('interface_id', ctypes.c_int), ('dxfer_direction', ctypes.c_int),
        ('cmd_len', ctypes.c_ubyte), ('mx_sb_len', ctypes.c_ubyte),
        ('iovec_count', ctypes.c_ushort), ('dxfer_len', ctypes.c_uint),
        ('dxferp', ctypes.c_void_p), ('cmdp', ctypes.c_void_p), ('sbp', ctypes.c_void_p),
        ('timeout', ctypes.c_uint), ('flags', ctypes.c_uint), ('pack_id', ctypes.c_uint),
        ('usr_ptr', ctypes.c_void_p),
        ('status', ctypes.c_ubyte), ('masked', ctypes.c_ubyte),
        ('msgout', ctypes.c_ubyte), ('sb_len_wr', ctypes.c_ubyte),
        ('host_status', ctypes.c_ushort), ('driver_status', ctypes.c_ushort),
        ('resid', ctypes.c_int), ('duration', ctypes.c_uint), ('info', ctypes.c_uint),
    ]


def sgio(path, cdb, indir=0, outdata=b'', timeout=3000):
    cdb = bytes(cdb)
    sb = ctypes.create_string_buffer(32)
    cmdb = ctypes.create_string_buffer(cdb, len(cdb) + 1)
    buf = ctypes.create_string_buffer(max(indir, len(outdata), 1))
    if outdata:
        ctypes.memmove(buf, outdata, len(outdata))
    r = Req()
    r.interface_id = ord('S')
    r.dxfer_direction = DIR_FROM_DEV if indir else (DIR_TO_DEV if outdata else DIR_NONE)
    r.cmd_len = len(cdb)
    r.mx_sb_len = 32
    r.dxfer_len = indir or len(outdata)
    r.dxferp = ctypes.addressof(buf)
    r.cmdp = ctypes.addressof(cmdb)
    r.sbp = ctypes.addressof(sb)
    r.timeout = timeout
    # O_RDWR: the kernel refuses DMA_NONE/DMA_TO_DEV commands on a read-only fd (EPERM).
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, SG_IO, r)
    finally:
        os.close(fd)
    return {'status': r.status, 'host_status': r.host_status, 'driver_status': r.driver_status,
            'resid': r.resid, 'duration': r.duration, 'info': r.info,
            'sb': bytes(sb.raw[:r.sb_len_wr]), 'data': bytes(buf.raw[:indir]) if indir else b''}


def show(res, verbose=False):
    print('  status 0x%02x host 0x%04x driver 0x%04x resid %d dur %dms info 0x%08x'
          % (res['status'], res['host_status'], res['driver_status'], res['resid'],
             res['duration'], res['info']))
    s = res['sb']
    if s:
        if (s[0] & 0x7f) in (0x70, 0x71, 0x72, 0x73) and len(s) > 13:
            print('  sense key %02x asc %02x ascq %02x  raw %s' % (s[2] & 0x0f, s[12], s[13], s.hex()))
        else:
            print('  sense raw %s' % s.hex())
    if res['data']:
        print('  data %d: %s' % (len(res['data']), res['data'][:64].hex(' ')))
        if verbose and len(res['data']) > 64:
            print('        %s' % res['data'][64:256].hex(' '))


def main():
    path, mode = sys.argv[1], sys.argv[2]
    argv = sys.argv[3:]
    verbose = '-v' in argv
    if mode == 'test':
        cdb, indir, out = bytes([0x00, 0, 0, 0, 0, 0]), 0, b''
    elif mode == 'inq':
        cdb, indir, out = bytes([0x12, 0, 0, 0, 96, 0]), 96, b''
    else:
        cdb = indir = 0
        out = b''
        i = 0
        toks = []
        while i < len(argv):
            a = argv[i]
            if a == '-i':
                indir = int(argv[i + 1], 0); i += 2
            elif a == '-o':
                out = bytes.fromhex(argv[i + 1]); i += 2
            elif a == '-t':
                i += 2
            elif a == '-v':
                i += 1
            else:
                toks.append(a); i += 1
        cdb = bytes.fromhex(''.join(toks))
    print('cdb %s%s' % (cdb.hex(' '), (' in=%d' % indir) if indir else ((' out=%d' % len(out)) if out else '')))
    try:
        show(sgio(path, cdb, indir, out), verbose)
    except OSError as e:
        print('  ioctl failed: %s (errno %d)' % (e.strerror, e.errno))


if __name__ == '__main__':
    main()
