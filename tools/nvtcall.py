#!/usr/bin/env python3
"""High-level helpers over the Novatek NvUSB channel (wire format: see nvtscsi.py).

  nvtcall.py [/dev/sg2] peek <va> [len]                 device RAM → host (opcode 0xc3)
  nvtcall.py [/dev/sg2] poke <va> <hexbytes>            host → device RAM (opcode 0xc4)
  nvtcall.py [/dev/sg2] cmd <grp> <fn> [ptr] [size] [-i N | -o hex]
                                                        opcode 0xc5, grp/fn = CDB[10]/CDB[11]
  nvtcall.py [/dev/sg2] call <fn_va> [hexarg]           BiCall fn3: the device jumps to fn_va
                                                        with a0 = buffer B
  nvtcall.py [/dev/sg2] line <module> <action> [args]   type a console line, print its reply;
                                                        -w <sec> to wait longer (default 0.5)
  nvtcall.py [/dev/sg2] clist [-j]                     dump the console command dictionary
                                                        (list head 0x80565300); -j also writes
                                                        ../notes/console_list.json

ctx singleton 0x805b0520 (static, magic 'MSDC'):
  +0x08 cache  +0x1c buffer A (cache+0x40)  +0x66c buffer B (A+0x10000)  +0x674 len B
  +0x38 fpCall  +0x67c HostLock  +0x680 run-busy  +0x24/0x28 SiCall get table/cnt
  +0x2c/0x30 SiCall set table/cnt  +0x3c BiCall proc registry  +0x44 DbgSys registry

BiCall (grp 1): 1 = copy A → B, 2 = GetProc by name in B, 3 = Call, 4 = copy B → A
SiCall (grp 2): 1, 2, 3;  DbgSys (grp 0) and the SiCall tables are unregistered in MSDC mode.
"""
import importlib.util
import os
import struct
import sys
import time

spec = importlib.util.spec_from_file_location(
    'nvtscsi', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nvtscsi.py'))
nvt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nvt)

CTX = 0x805b0520


def cdb(op, ptr=0, size=0, grp=0, fn=0):
    return struct.pack('>BBII', op, 7, ptr, size) + bytes([grp, fn]) + b'\0\0\0\0'


class Dev:
    def __init__(self, path):
        self.path = path
        self.buf_a = self.word(CTX + 0x1c)      # heap; read live, it moves with the allocation order

    def xfer(self, op, ptr=0, size=0, grp=0, fn=0, indir=0, out=b''):
        try:
            r = nvt.sgio(self.path, cdb(op, ptr, size, grp, fn), indir, out)
        except OSError as e:
            sys.exit('%s: %s' % (self.path, e.strerror))
        s = r['sb']
        if s and (s[0] & 0x7f) in (0x70, 0x71) and len(s) > 13:
            print('  CHECK CONDITION sense %02x/%02x/%02x' % (s[2] & 0x0f, s[12], s[13]))
        elif r['status']:
            print('  status 0x%02x host 0x%04x' % (r['status'], r['host_status']))
        return r

    def peek(self, va, n):
        return self.xfer(0xc3, va, n, indir=n)['data']

    def word(self, va):
        return struct.unpack('<I', self.peek(va, 4))[0]

    def call(self, fn_va, arg=b''):
        """BiCall fn3: fpCall = fn_va, memcpy(buffer B <- buffer A, ctx+0x18), jump with a0 = B.
        Buffer A is plain bytes (no packet header of its own) and Verify requires the header size
        to equal the DATA-IN length, so the transfer size *is* the argument length."""
        n = max(len(arg), 4)
        if arg:
            self.xfer(0xc4, self.buf_a, n, out=arg + b'\0' * (n - len(arg)))
        return self.xfer(0xc5, fn_va, n, 1, 3, indir=n)

    def line(self, text, wait=0.5, readback=0x100):
        """Type a console line: executor 0x80080390 runs in the poll task with a0 = buffer B.
        Only the last printf survives in the console buffer, so poll and keep distinct lines."""
        self.call(EXEC, text.encode() + b'\0')
        lines, t0 = [], time.time()
        while time.time() - t0 < wait:
            s = self.peek(CON_BUF, readback).split(b'\0')[0].decode('latin1').strip()
            if s and s not in lines:
                lines.append(s)
        busy = self.word(CON_BUSY)
        if busy:
            self.xfer(0xc4, CON_BUSY, 4, out=b'\0' * 4)        # the poll task clears it only on return
            lines.append('(note: run-busy flag was stuck, cleared)')
        return '\n'.join(lines)


class Reader:
    """Chunked cached device-RAM reader: one SG_IO per chunk, not per field."""

    def __init__(self, dev, chunk=0x100):
        self.dev, self.chunk, self.pages = dev, chunk, {}

    def rd(self, va, n):
        out = b''
        while n > 0:
            pg = va - va % self.chunk
            if pg not in self.pages:
                self.pages[pg] = self.dev.peek(pg, self.chunk)
            off = va - pg
            take = min(n, self.chunk - off)
            out += self.pages[pg][off:off + take]
            va += take
            n -= take
        return out

    def word(self, va):
        return struct.unpack('<I', self.rd(va, 4))[0]

    def cstr(self, va, limit=64):
        if not (0x80000000 <= va < 0x80800000):
            return None
        s = self.rd(va, limit).split(b'\0')[0]
        if not s or any(c < 0x20 or c > 0x7e for c in s):
            return None
        return s.decode()


LIST_HEAD, LIST_TAIL, LIST_CNT = 0x80565300, 0x80565304, 0x80565308
EXEC = 0x80080390          # line executor: a0 = "<module> <action> <args>"
CON_BUF = 0x805650b4       # every printf formats here before the mux at 0x8007f2a4: the last line only
CON_BUSY = CTX + 0x680


def console_list(r):
    """Walk the command-module list; nodes are {name, next, desc} + 12-byte {name, fn, help}."""
    modules, t, seen = [], r.word(LIST_HEAD), set()
    while t and t not in seen and len(seen) < 256:
        seen.add(t)
        name = r.cstr(r.word(t))
        desc = r.cstr(r.word(t + 8))
        cmds, e = [], t + 0xc
        while True:
            fn, hp = r.word(e + 4), r.word(e + 8)
            cname = r.cstr(r.word(e))
            if not cname:
                break
            cmds.append((cname, fn, r.cstr(hp, 80)))
            e += 0xc
        modules.append((t, name, desc, cmds))
        t = r.word(t + 4)
    return modules


def hexdump(b, off=0):
    for i in range(0, len(b), 16):
        row = b[i:i + 16]
        print('%08x  %-47s  %s' % (off + i, ' '.join('%02x' % c for c in row),
                                   ''.join(chr(c) if 0x20 <= c < 0x7f else '.' for c in row)))


if __name__ == '__main__':
    argv = sys.argv[1:]
    path = '/dev/sg2'
    if argv and argv[0].startswith('/dev/'):
        path, argv = argv[0], argv[1:]
    dev = Dev(path)
    mode = argv[0]
    flags = argv[1:]
    inb = int(flags[flags.index('-i') + 1], 0) if '-i' in flags else 0
    out = bytes.fromhex(flags[flags.index('-o') + 1]) if '-o' in flags else b''
    args = []
    i = 0
    while i < len(flags):
        if flags[i].startswith('-'):
            i += 2
        else:
            args.append(flags[i])
            i += 1
    if mode in ('peek', 'w'):
        n = int(args[1], 0) if len(args) > 1 else 64
        hexdump(dev.peek(int(args[0], 0), n), int(args[0], 0))
    elif mode == 'word':
        for a in args:
            print('%08x %08x' % (int(a, 0), dev.word(int(a, 0))))
    elif mode == 'poke':
        data = bytes.fromhex(args[1])
        dev.xfer(0xc4, int(args[0], 0), len(data), out=data)
    elif mode == 'cmd':
        grp, fn = int(args[0], 0), int(args[1], 0)
        ptr = int(args[2], 0) if len(args) > 2 else 0
        size = int(args[3], 0) if len(args) > 3 else (inb or len(out))
        r = dev.xfer(0xc5, ptr, size, grp, fn, inb, out)
        print('  reply', r['data'].hex(' ') or '(none)')
    elif mode == 'line':
        print(dev.line(' '.join(args),
                       float(flags[flags.index('-w') + 1]) if '-w' in flags else 0.5))
    elif mode == 'clist':
        mods = console_list(Reader(dev))
        print('# head 0x%08x tail 0x%08x count 0x%x, walked %d modules' % (
            dev.word(LIST_HEAD), dev.word(LIST_TAIL), dev.word(LIST_CNT), len(mods)))
        for va, name, desc, cmds in mods:
            print('\n[%s]  table 0x%08x  %s' % (name, va, desc or ''))
            for cname, fn, help_ in cmds:
                print('    %-18s fn=0x%08x  %s' % (cname, fn, help_ or ''))
        if '-j' in flags:
            import json
            dst = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '..', 'notes', 'console_list.json')
            json.dump([[m[0], m[1], m[2], m[3]] for m in mods],
                      open(dst, 'w'), indent=1, ensure_ascii=False)
    elif mode == 'call':
        arg = bytes.fromhex(args[1]) if len(args) > 1 else b''
        r = dev.call(int(args[0], 0), arg)
        print('  reply', r['data'].hex(' ') or '(none)')
    else:
        sys.exit(__doc__)
