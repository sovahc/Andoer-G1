#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int readvar(const unsigned char* in, unsigned int* val) {
    unsigned int v = 0, n = 0, b;
    do { b = in[n]; v = (v << 7) | (b & 0x7f); n++; } while (b & 0x80);
    *val = v; return n;
}

int main(int argc, char** argv) {
    FILE* f = fopen(argv[1], "rb");
    fseek(f, 0, SEEK_END); long fs = ftell(f);
    long start = strtol(argv[2], 0, 0);
    long maxin = argc > 3 ? strtol(argv[3], 0, 0) : fs - start;
    fseek(f, start, SEEK_SET);
    unsigned char* in = malloc(maxin);
    fread(in, 1, maxin, f); fclose(f);
    long cap = argc > 4 ? strtol(argv[4], 0, 0) : 0x800000;
    unsigned char* out = malloc(cap);
    unsigned char marker = in[0];
    unsigned int ip = 1, op = 0, len, off;
    while (ip < (unsigned)maxin && op + 0x10000 < (unsigned)cap) {
        unsigned char b = in[ip++];
        if (b != marker) out[op++] = b;
        else if (in[ip] == 0) { out[op++] = marker; ip++; }
        else {
            ip += readvar(in + ip, &len);
            ip += readvar(in + ip, &off);
            if (off == 0 || off > op || op + len > (unsigned)cap) { fprintf(stderr, "bail at ip=%x op=%x len=%u off=%u\n", start+ip, op, len, off); break; }
            for (unsigned i = 0; i < len; i++) { out[op] = out[op - off]; op++; }
        }
    }
    FILE* o = fopen(argv[argc-1], "wb");
    fwrite(out, 1, op, o); fclose(o);
    fprintf(stderr, "marker=%02x in=%ld out=%u (%.1f%%)\n", marker, maxin, op, 100.0*op/maxin);
    return 0;
}
