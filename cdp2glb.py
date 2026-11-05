#!/usr/bin/env python3
"""
cdp2glb.py - convert Trainz .cdp (ACS$ / CHUMP) packages to glTF binary (.glb) for Blender.

    python cdp2glb.py "BN SW1200.cdp" out_folder
    python cdp2glb.py "BN SW1200.cdp" out_folder --list
    python cdp2glb.py "BN SW1200.cdp" out_folder --extract      (also dump every file in the package)
    python cdp2glb.py "BN SW1200.cdp" out_folder --all-lods     (export every LOD mesh, not just the best)
    python cdp2glb.py "BN SW1200.cdp" out_folder --asset sw1200 (only assets whose name/kuid contains this text)

Then in Blender: File > Import > glTF 2.0 (.glb/.gltf).

Tip: a big package holds hundreds of assets.  Run with --list first, then export
one or a few with --asset <text>.

Pure Python 3, no third-party packages.  The container + compression format
(LZSS / adaptive-Huffman) follows the community reverse-engineering in `cdptool`
and CDP Explorer; the .im mesh reader was worked out from your sample files.

Trainz models are Z-up / Y-forward (same as Blender); the glb is rotated to glTF's
Y-up convention so Blender's importer puts them back upright.
"""
import sys, os, re, json, struct, zlib, argparse, time

# Windows consoles (cp1252) crash on odd characters in asset names -> never let print() kill the run
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

LEAF = 0x8000
REBUILD = 0x7FFF
UNPROC = 0xFFFFFFFE
LENGTH_TABLE = [(0,0),(1,0),(2,0),(3,0),(4,0),(5,1),(7,1),(9,2),(13,2),(17,3),
                (25,3),(33,4),(49,4),(65,5),(97,5),(129,7)]
EOF_SYM = 272


class BitReader:
    def __init__(self, data):
        self.data = data
        self.nbits = len(data) * 8
        self.pos = 0

    def read_bit(self):
        if self.pos >= self.nbits:
            return 0
        b = (self.data[self.pos >> 3] >> (self.pos & 7)) & 1
        self.pos += 1
        return b

    def read_bits(self, n):
        if n == 0:
            return 0
        avail = min(self.nbits - self.pos, n)
        if avail <= 0:
            return 0
        v = 0
        got = 0
        pos = self.pos
        data = self.data
        while got < avail:
            byte = data[pos >> 3]
            off = pos & 7
            take = min(8 - off, avail - got)
            v |= ((byte >> off) & ((1 << take) - 1)) << got
            got += take
            pos += take
        self.pos = pos
        return v


class AdaptiveHuffman:
    def __init__(self, nraw):
        n = (nraw + 1) & 0xFFE
        total = n * 2 - 1
        self.n = n
        self.total = total
        self.root = total - 1
        self.freq = [0] * total
        self.parent = [0] * total
        self.child = [0] * total
        self.s2n = [0] * n
        for i in range(n):
            self.freq[i] = 1
            self.child[i] = i | LEAF
            self.s2n[i] = i
        j = 0
        for k in range(n, total):
            self.freq[k] = self.freq[j] + self.freq[j + 1]
            self.child[k] = j
            self.parent[j] = k
            self.parent[j + 1] = k
            j += 2
        self.parent[self.root] = self.root

    def decode(self, br):
        child = self.child
        v = child[self.root]
        depth = 0
        while not (v & LEAF):
            idx = v + br.read_bit()
            if idx >= self.total:
                raise ValueError("tree index out of bounds")
            v = child[idx]
            depth += 1
            if depth > 50:
                raise ValueError("tree depth exceeded")
        sym = v & ~LEAF
        self.update(sym)
        return sym

    def update(self, symbol):
        freq, parent, child, s2n = self.freq, self.parent, self.child, self.s2n
        total = self.total
        if freq[self.root] > REBUILD:
            self.rebuild()
        i = s2n[symbol]
        while True:
            freq[i] += 1
            f = freq[i]
            if i + 1 < total and freq[i + 1] < f:
                j = i + 2
                while j < total and freq[j] < f:
                    j += 1
                j -= 1
                ci = child[i]
                cj = child[j]
                if not (cj & LEAF):
                    parent[cj] = i
                    parent[cj + 1] = i
                else:
                    s2n[cj & ~LEAF] = i
                if not (ci & LEAF):
                    parent[ci] = j
                    parent[ci + 1] = j
                else:
                    s2n[ci & ~LEAF] = j
                freq[i], freq[j] = freq[j], freq[i]
                freq[j] = f
                child[i], child[j] = child[j], child[i]
                i = j
            p = parent[i]
            if p == i or p == self.root:
                break
            i = p
        freq[self.root] += 1

    def rebuild(self):
        freq, parent, child, s2n = self.freq, self.parent, self.child, self.s2n
        root = self.root
        remaining = self.n
        for i in range(root):
            if child[i] & LEAF:
                freq[i] = (freq[i] + 1) >> 1
            parent[i] = UNPROC
        parent[root] = UNPROC
        pair_scan = 0
        node_scan = 0
        while True:
            found = False
            while pair_scan < root:
                if parent[pair_scan] == UNPROC:
                    found = True
                    break
                pair_scan += 1
            if not found or pair_scan >= root:
                parent[root] = 0xFFFFFFFF
                return
            left = pair_scan
            right = pair_scan + 1
            pair_scan += 2
            while node_scan <= root:
                if child[node_scan] & LEAF:
                    remaining -= 1
                elif parent[node_scan] == UNPROC:
                    break
                node_scan += 1
            slot = node_scan
            node_scan += 1
            next_scan = node_scan
            new_freq = freq[left] + freq[right]
            insert_at = slot
            if slot > 0 and freq[slot - 1] > new_freq:
                scan = slot - 1
                while scan >= 0 and freq[scan] > new_freq:
                    c = child[scan]
                    if not (c & LEAF):
                        parent[c] += 1
                        parent[c + 1] += 1
                    else:
                        s2n[c & ~LEAF] += 1
                    scan -= 1
                pos = scan + 1
                count = slot - pos
                if count > 0:
                    freq[pos + 1:pos + 1 + count] = freq[pos:pos + count]
                    child[pos + 1:pos + 1 + count] = child[pos:pos + count]
                insert_at = pos
            elif remaining > 0:
                scan_pos = next_scan
                dst = slot
                nxt = next_scan
                while True:
                    while scan_pos < root:
                        if child[scan_pos] & LEAF:
                            break
                        scan_pos += 1
                    if scan_pos >= root or freq[scan_pos] >= new_freq:
                        break
                    freq[dst] = freq[scan_pos]
                    child[dst] = child[scan_pos]
                    parent[dst] = UNPROC
                    s2n[child[scan_pos] & ~LEAF] = dst
                    dst = nxt
                    child[scan_pos] = 0
                    nxt += 1
                    node_scan = nxt
                    remaining -= 1
                    if remaining <= 0:
                        break
                    scan_pos += 1
                insert_at = dst
            freq[insert_at] = new_freq
            child[insert_at] = left
            parent[left] = insert_at
            parent[right] = insert_at


def copy_match(out, dist, length, limit):
    length = min(length, limit - len(out))
    if length <= 0:
        return
    if dist > len(out):                       # reference before start of data -> zeros
        pad = min(dist - len(out), length)
        out.extend(b"\0" * pad)
        length -= pad
        if length <= 0:
            return
    start = len(out) - dist
    if dist >= length:
        out.extend(out[start:start + length])
    else:                                     # overlapping run: repeat the pattern
        pat = bytes(out[start:])
        out.extend((pat * (length // dist + 1))[:length])


def mode_params(mode):
    if mode == 0:
        return 8, 16, 4
    if mode == 1:
        return 12, 64, 6
    if mode == 2:
        return 14, 64, 8
    raise ValueError("bad mode %d" % mode)


def decompress(comp, usize):
    if not comp:
        raise ValueError("empty")
    first = comp[0]
    mode = (first >> 4) & 3
    level = first & 0xF
    if level == 0:
        return bytes(comp[1:1 + usize])
    dbits, ndsym, dextra = mode_params(mode)
    if level < 7:
        return _bitstream(comp[1:], usize, dbits, dextra)
    return _huffman(comp[1:], usize, dbits, ndsym, dextra)


def _huffman(stream, usize, dbits, ndsym, dextra):
    dmask = (1 << dbits) - 1
    lit = AdaptiveHuffman(273)
    dst = AdaptiveHuffman(ndsym)
    br = BitReader(stream)
    out = bytearray()
    while len(out) < usize:
        sym = lit.decode(br)
        if sym == EOF_SYM:
            break
        if sym > EOF_SYM:
            raise ValueError("bad symbol")
        if sym < 256:
            out.append(sym)
        else:
            base, extra = LENGTH_TABLE[sym - 256]
            ev = br.read_bits(extra) if extra else 0
            mlen = base + ev + 3
            ds = dst.decode(br)
            de = br.read_bits(dextra)
            d = ((ds << dextra) | de) & dmask
            if d == 0:
                d = 1
            copy_match(out, d, mlen, usize)
    return bytes(out[:usize])


def _bitstream(stream, usize, dbits, dextra):
    dmask = (1 << dbits) - 1
    len_over = ((1 << (16 - dbits)) - 1) if dbits < 16 else 0
    shift = min(dbits, 15)
    br = BitReader(stream)
    out = bytearray()
    if dbits == 8:
        while len(out) < usize:
            flags = br.read_bits(32)
            mask = 1
            while mask and len(out) < usize:
                if not (flags & mask):
                    out.append(br.read_bits(8))
                    mask = (mask << 1) & 0xFFFFFFFF
                else:
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    lc = 1 if flags & mask else 0
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    lc |= 2 if flags & mask else 0
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    lc |= 4 if flags & mask else 0
                    if lc == 7:
                        lc = br.read_bits(8)
                    d = br.read_bits(8) or 1
                    copy_match(out, d, lc + 3, usize)
                    mask = (mask << 1) & 0xFFFFFFFF
    else:
        while len(out) < usize:
            flags = br.read_bits(32)
            mask = 1
            while mask and len(out) < usize:
                if not (flags & mask):
                    mask = (mask << 1) & 0xFFFFFFFF
                    if not mask:
                        break
                    if not (flags & mask):
                        out.append(br.read_bits(8))
                    else:
                        run = br.read_bits(8) + 3
                        for _ in range(run):
                            if len(out) >= usize:
                                break
                            out.append(br.read_bits(8))
                    mask = (mask << 1) & 0xFFFFFFFF
                else:
                    word = br.read_bits(16)
                    d = (word & dmask) or 1
                    lc = (word >> shift) & len_over
                    if lc == len_over:
                        lc = br.read_bits(8)
                    copy_match(out, d, lc + 3, usize)
                    mask = (mask << 1) & 0xFFFFFFFF
    return bytes(out[:usize])


# =====================================================================
#  CDP container
# =====================================================================
class Node:
    __slots__ = ("name", "type", "off", "end", "kids")
    def __init__(self, name, type_, off, end):
        self.name, self.type, self.off, self.end, self.kids = name, type_, off, end, []


def parse_tree(data, off, end):
    nodes = []
    while off + 6 <= end:
        tag_len = struct.unpack_from("<I", data, off)[0]
        start = off
        off += 4
        nl = data[off]
        off += 1
        if nl:
            if off + nl > end:
                break
            name = bytes(data[off:off + nl - 1]).decode("utf-8", "replace")
            off += nl
        else:
            name = ""
        if off >= end:
            break
        ttype = data[off]
        off += 1
        vend = start + 4 + tag_len
        if vend > end:
            break
        n = Node(name, ttype, off, vend)
        if ttype == 0:
            n.kids = parse_tree(data, off, vend)
        nodes.append(n)
        off = vend
    return nodes


class Asset:
    def __init__(self, name):
        self.name = name
        self.info = {}
        self.files = {}      # filename -> Node (binary)
        self.node = None
        self.cache = {}

    def has(self, fname):
        return fname.lower() in self.files


class Package:
    def __init__(self, data):
        if bytes(data[:4]) != b"ACS$":
            raise SystemExit("Not a Trainz ACS$ package.")
        self.data = data
        self.assets = []
        tree = parse_tree(data, 16, len(data))
        self._walk(tree)
        self._global = {}
        for a in self.assets:
            for k in a.files:
                self._global.setdefault(k, (a, k))

    def _walk(self, nodes):
        for n in nodes:
            if n.type == 0:
                files = [k for k in n.kids if k.type == 0 and k.name == "files"]
                if files:
                    a = Asset(n.name)
                    for k in n.kids:
                        if k.type == 3:
                            a.info[k.name] = bytes(self.data[k.off:k.end]).split(b"\0")[0].decode("utf-8", "replace")
                    def collect(kids):
                        for f in kids:
                            if f.type == 0:
                                collect(f.kids)
                            elif f.type == 4:
                                a.files.setdefault(f.name.lower().replace("\\", "/").split("/")[-1], f)
                    collect(files[0].kids)
                    a.node = n
                    self.assets.append(a)
                else:
                    self._walk(n.kids)

    def read(self, asset, fname):
        key = fname.lower()
        if key in asset.cache:
            return asset.cache[key]
        n = asset.files[key]
        us = struct.unpack_from("<I", self.data, n.off)[0]
        out = decompress(bytes(self.data[n.off + 4:n.end]), us)
        if len(asset.cache) < 16:
            asset.cache[key] = out
        return out

    def find(self, asset, fname):
        """look in the asset first, then any other asset (dependencies). Returns (asset, name) or None"""
        k = fname.lower()
        if k in asset.files:
            return asset, k
        return self._global.get(k)


# =====================================================================
#  Images -> PNG   (TGA / BMP decoded here, PNG/JPG passed through)
# =====================================================================
def png_encode(w, h, rgba, has_alpha=True):
    ch = 4 if has_alpha else 3
    raw = bytearray()
    stride = w * ch
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if has_alpha else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def decode_tga(b):
    idlen, cmtype, imtype = b[0], b[1], b[2]
    cmlen = struct.unpack_from("<H", b, 5)[0]
    cmbits = b[7]
    w, h = struct.unpack_from("<HH", b, 12)
    bpp, desc = b[16], b[17]
    pos = 18 + idlen
    cmap = None
    if cmtype:
        cb = (cmbits + 7) // 8
        cmap = [bytes(b[pos + i * cb:pos + (i + 1) * cb]) for i in range(cmlen)]
        pos += cmlen * cb
    px = (bpp + 7) // 8
    need = w * h * px
    if imtype in (9, 10, 11):
        out = bytearray()
        while len(out) < need and pos < len(b):
            c = b[pos]
            pos += 1
            cnt = (c & 0x7F) + 1
            if c & 0x80:
                out += b[pos:pos + px] * cnt
                pos += px
            else:
                out += b[pos:pos + cnt * px]
                pos += cnt * px
        src = bytes(out[:need])
    else:
        src = bytes(b[pos:pos + need])
    if len(src) < need:                      # truncated data: pad so slicing never mismatches
        src = src + b"\0" * (need - len(src))
    rgba = bytearray(w * h * 4)
    if imtype in (1, 9) and cmap:
        for i in range(w * h):
            c = cmap[src[i * px]] if px == 1 else cmap[struct.unpack_from("<H", src, i * px)[0]]
            rgba[i * 4:i * 4 + 4] = bytes((c[2], c[1], c[0], c[3] if len(c) > 3 else 255))
    elif imtype in (2, 10) and bpp == 32:
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = src[2::4], src[1::4], src[0::4], src[3::4]
    elif imtype in (2, 10) and bpp == 24:
        rgba[0::4], rgba[1::4], rgba[2::4] = src[2::3], src[1::3], src[0::3]
        rgba[3::4] = b"\xff" * (w * h)
    elif imtype in (2, 10) and bpp == 16:
        for i in range(w * h):
            v = src[i * 2] | (src[i * 2 + 1] << 8)
            rgba[i * 4:i * 4 + 4] = bytes((((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31, 255))
    elif imtype in (3, 11):
        rgba[0::4] = rgba[1::4] = rgba[2::4] = src[0::px]
        rgba[3::4] = b"\xff" * (w * h)
    else:
        raise ValueError("unsupported TGA type %d/%d" % (imtype, bpp))
    if not (desc & 0x20):  # bottom-up -> flip
        rows = [rgba[y * w * 4:(y + 1) * w * 4] for y in range(h)]
        rgba = bytearray(b"".join(reversed(rows)))
    return w, h, rgba


def decode_bmp(b):
    off = struct.unpack_from("<I", b, 10)[0]
    hs = struct.unpack_from("<I", b, 14)[0]
    w, h = struct.unpack_from("<ii", b, 18)
    bpp = struct.unpack_from("<H", b, 28)[0]
    comp = struct.unpack_from("<I", b, 30)[0]
    ncol = struct.unpack_from("<I", b, 46)[0]
    if comp not in (0, 3):
        raise ValueError("compressed BMP unsupported")
    top_down = h < 0
    h = abs(h)
    pal = None
    if bpp <= 8:
        ncol = ncol or (1 << bpp)
        pal = [bytes(b[14 + hs + i * 4:14 + hs + i * 4 + 4]) for i in range(ncol)]
    rowb = ((w * bpp + 31) // 32) * 4
    rgba = bytearray(w * h * 4)
    for y in range(h):
        row = b[off + y * rowb:off + (y + 1) * rowb]
        dy = y if top_down else h - 1 - y
        o = dy * w * 4
        if bpp == 24:
            for x in range(w):
                rgba[o + x * 4:o + x * 4 + 4] = bytes((row[x * 3 + 2], row[x * 3 + 1], row[x * 3], 255))
        elif bpp == 32:
            for x in range(w):
                rgba[o + x * 4:o + x * 4 + 4] = bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4], 255))
        elif bpp == 8:
            for x in range(w):
                p = pal[row[x]]
                rgba[o + x * 4:o + x * 4 + 4] = bytes((p[2], p[1], p[0], 255))
        elif bpp == 4:
            for x in range(w):
                v = (row[x >> 1] >> (4 if x % 2 == 0 else 0)) & 15
                p = pal[v]
                rgba[o + x * 4:o + x * 4 + 4] = bytes((p[2], p[1], p[0], 255))
        elif bpp == 1:
            for x in range(w):
                p = pal[(row[x >> 3] >> (7 - (x & 7))) & 1]
                rgba[o + x * 4:o + x * 4 + 4] = bytes((p[2], p[1], p[0], 255))
        else:
            raise ValueError("unsupported BMP depth %d" % bpp)
    return w, h, rgba


def decode_image(name, data):
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "tga":
        return decode_tga(data)
    if ext == "bmp":
        return decode_bmp(data)
    return None


# =====================================================================
#  Trainz .im mesh
# =====================================================================
class Primitive:
    pass


def read_im(b):
    if b[:4] != b"JIRF":
        raise ValueError("not a Trainz .im (no JIRF header)")
    total = struct.unpack_from("<I", b, 4)[0] + 8
    prims, attach = [], []
    p = 12
    while p + 8 <= min(total, len(b)):
        tag = bytes(b[p:p + 4])
        sz = struct.unpack_from("<I", b, p + 4)[0]
        body = b[p + 8:p + 8 + sz]
        if tag == b"CHNK":
            try:
                pr = read_chnk(body)
            except struct.error:
                pr = None
            if pr:
                prims.append(pr)
        elif tag == b"ATCH":
            try:
                q = 8
                cnt = struct.unpack_from("<I", body, 4)[0]
                for _ in range(cnt):
                    n = struct.unpack_from("<I", body, q)[0]
                    name = bytes(body[q + 4:q + 4 + n]).split(b"\0")[0].decode("latin1")
                    m = struct.unpack_from("<12f", body, q + 4 + n)
                    attach.append((name, m))
                    q += 4 + n + 48
            except struct.error:
                pass          # damaged attach table: keep the mesh anyway
        p += 8 + sz
    return prims, attach


def _geom_standard(sub, h):
    nv, ni, nf = h[4], h[6], h[7]
    o = 40
    V = struct.unpack_from("<%df" % (nv * 5), sub, o)
    o += nv * 20
    idx = struct.unpack_from("<%dH" % ni, sub, o)
    o += ni * 2
    o += nf * 12
    N = None
    if o + nv * 12 <= len(sub):
        N = struct.unpack_from("<%df" % (nv * 3), sub, o)
    return nv, V, idx, N


def _geom_ok(g):
    nv, V, idx, N = g
    return nv > 0 and len(idx) >= 3 and max(idx) < nv


def _geom_search(sub, h):
    """Layout not the usual one (animated / special meshes): work it out from the chunk size.
    Tries vertex stride = (size - indices - face data) / nv, 16- and 32-bit indices,
    indices before or after the vertices.  Returns (nv, V, idx, None) or None."""
    nv = h[4]
    if not 0 < nv <= 65536:
        return None
    sz = len(sub)
    nis = sorted({x for i, x in enumerate(h) if i != 4 and 3 <= x <= 400000 and x % 3 == 0})
    nfs = sorted({0} | {x for x in h if x <= 100000})
    for isize in (2, 4):
        for ni in nis:
            for nf in nfs:
                rem = sz - 40 - ni * isize - nf * 12
                if rem < nv * 20 or rem % nv:
                    continue
                stride = rem // nv
                if stride % 4:
                    continue
                for idx_first in (False, True):
                    try:
                        vo = 40 + (ni * isize if idx_first else 0)
                        io = 40 if idx_first else 40 + nv * stride
                        V = []
                        for i in range(nv):
                            V.extend(struct.unpack_from("<5f", sub, vo + i * stride))
                        if not all(c == c and abs(c) < 1e6 for c in V):
                            continue
                        idx = struct.unpack_from("<%d%s" % (ni, "H" if isize == 2 else "I"), sub, io)
                    except struct.error:
                        continue
                    if idx and max(idx) < nv:
                        return nv, tuple(V), idx, None
    return None


def _unit_normals(sub, o, nv):
    """True if the nv*12 bytes at o look like unit-length normals"""
    if o < 0 or o + nv * 12 > len(sub):
        return False
    for i in range(0, nv, max(1, nv // 8)):
        x, y, z = struct.unpack_from("<3f", sub, o + i * 12)
        l2 = x * x + y * y + z * z
        if not (0.8 < l2 < 1.25):
            return False
    return True


def _vert_score(V, nv):
    """how plausible is this vertex block: UVs small, and not 'pad bytes read as x,y'"""
    good = 0
    zero_xy = 0
    for i in range(nv):
        x, y, z, u, v = V[i * 5:i * 5 + 5]
        if abs(u) <= 16 and abs(v) <= 16:
            good += 1
        if x == 0.0 and y == 0.0:
            zero_xy += 1
    sc = good / nv
    if nv > 3 and zero_xy > nv * 0.5:
        sc -= 1.0
    return sc


def _geom_gap(sub, h):
    """Layout seen in animated / extra-data meshes: the usual blocks, but h[9] extra bytes
    sit before the vertices or between vertices and indices (the header's last number).
    Both placements are tried and the one whose vertices look sane wins."""
    nv, ni, nf, extra = h[4], h[6], h[7], h[9]
    if not 0 < nv <= 65536 or ni < 3:
        return None
    gaps = sorted({0, 4, 8, 12, 16, 24})
    first = [(extra, 0), (0, extra)]
    rest = [(a, b) for a in sorted({0, extra, 4, 8, 12, 16}) for b in gaps if (a, b) not in first]
    best = None
    for k, (pre, gap) in enumerate(first + rest):
        vo = 40 + pre
        io = vo + nv * 20 + gap
        if io + ni * 2 > len(sub):
            continue
        try:
            V = struct.unpack_from("<%df" % (nv * 5), sub, vo)
            idx = struct.unpack_from("<%dH" % ni, sub, io)
        except struct.error:
            continue
        if not all(c == c and abs(c) < 1e5 for c in V) or max(idx) >= nv:
            continue
        sc = _vert_score(V, nv)
        if best is None or sc > best[0] + 1e-9:
            best = (sc, V, idx, io)
        if k >= 1 and best is not None:      # compared the two main candidates; accept
            break
    if best is None:
        return None
    _, V, idx, io = best
    N = None
    base = io + ni * 2 + nf * 12
    for g2 in (0, extra, 4, 8, 12, 16):
        for blk in (0, nv * 12):             # normals first, or a block before them
            o = base + g2 + blk
            if _unit_normals(sub, o, nv):
                N = struct.unpack_from("<%df" % (nv * 3), sub, o)
                break
        if N:
            break
    return nv, V, idx, N


def _score_normals(V, idx, N, nv):
    """compare vertex normals with the real face normals of the triangles.
    returns (mean |cos|, mean signed cos); ~1.0 / +-1.0 means the block really holds normals"""
    n = len(idx) // 3
    step = max(1, n // 300)
    tot = sgn = 0.0
    cnt = 0
    for t in range(0, n, step):
        a, b, c = idx[t * 3:t * 3 + 3]
        ax, ay, az = V[a * 5:a * 5 + 3]
        ux, uy, uz = V[b * 5] - ax, V[b * 5 + 1] - ay, V[b * 5 + 2] - az
        wx, wy, wz = V[c * 5] - ax, V[c * 5 + 1] - ay, V[c * 5 + 2] - az
        fx, fy, fz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        fl = (fx * fx + fy * fy + fz * fz) ** 0.5
        nx = N[a * 3] + N[b * 3] + N[c * 3]
        ny = N[a * 3 + 1] + N[b * 3 + 1] + N[c * 3 + 1]
        nz = N[a * 3 + 2] + N[b * 3 + 2] + N[c * 3 + 2]
        nl = (nx * nx + ny * ny + nz * nz) ** 0.5
        if fl < 1e-12 or nl < 1e-9:
            continue
        d = (fx * nx + fy * ny + fz * nz) / (fl * nl)
        tot += abs(d)
        sgn += d
        cnt += 1
    return (tot / cnt, sgn / cnt) if cnt else (0.0, 0.0)


def _choose_normals(sub, h, g):
    """find the real vertex-normal block (not tangents / face data) and fix winding to match it"""
    nv, V, idx, N0 = g
    ni, nf, extra = len(idx), h[7], h[9]
    base0 = 40 + nv * 20 + ni * 2
    offs = set()
    for e in (0, extra, extra * 2, 4, 8, 12, 16):
        for blk in (0, 1, 2):
            for f in (nf * 12, 0):
                offs.add(base0 + f + e + blk * nv * 12)
    best = None
    for o in sorted(offs):
        if not _unit_normals(sub, o, nv):
            continue
        N = struct.unpack_from("<%df" % (nv * 3), sub, o)
        sc = _score_normals(V, idx, N, nv)
        if best is None or sc[0] > best[0][0] + 1e-9:
            best = (sc, N, o)
    if best is None or best[0][0] < 0.5:
        return nv, V, idx, None, "no usable normals (flat shading)"
    (ab, sg), N, o = best
    info = "normals@%d match=%.2f" % (o, ab)
    if sg < -0.3:                                   # winding opposite to normals -> flip triangles
        idx = list(idx)
        for t in range(0, len(idx) - 2, 3):
            idx[t + 1], idx[t + 2] = idx[t + 2], idx[t + 1]
        idx = tuple(idx)
        info += ", winding flipped"
    return nv, V, idx, N, info


def parse_geom(sub):
    h = struct.unpack_from("<10I", sub, 0)
    g = None
    try:
        g = _geom_standard(sub, h)
    except (struct.error, ValueError):
        pass
    if g and _geom_ok(g):
        return _finish_geom(sub, h, g)
    alt = None
    for fn in (_geom_gap, _geom_search):
        try:
            alt = fn(sub, h)
        except Exception:
            alt = None
        if alt:
            break
    g = alt or g
    if g and _geom_ok(g):
        return _finish_geom(sub, h, g)
    return g, (h, len(sub), "")


def _finish_geom(sub, h, g):
    try:
        nv, V, idx, N, info = _choose_normals(sub, h, g)
        return (nv, V, idx, N), (h, len(sub), info)
    except Exception as e:
        return (g[0], g[1], g[2], None), (h, len(sub), "normals failed: %s" % e)


def read_chnk(body):
    pr = Primitive()
    pr.mat_name, pr.textures, pr.verts = "", {}, None
    q = 8
    while q + 8 <= len(body):
        tag = bytes(body[q:q + 4])
        sz = struct.unpack_from("<I", body, q + 4)[0]
        sub = body[q + 8:q + 8 + sz]
        if tag == b"MATL":
            try:
                nl = struct.unpack_from("<I", sub, 4)[0]
                pr.mat_name = bytes(sub[8:8 + nl]).split(b"\0")[0].decode("latin1")
                pos = 8 + nl + 64
                cnt = struct.unpack_from("<I", sub, pos)[0]
                pos += 4
                for _ in range(cnt):
                    ttype, ln = struct.unpack_from("<II", sub, pos)
                    path = bytes(sub[pos + 8:pos + 8 + ln]).split(b"\0")[0].decode("latin1")
                    pos += 8 + ln + 4
                    pr.textures[ttype] = path
            except Exception:
                pass
        elif tag == b"GEOM":
            pr.verts, pr.dbg = parse_geom(sub)
        q += 8 + sz
    return pr if pr.verts else None


# =====================================================================
#  glTF writer
# =====================================================================
class GLB:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accs, self.meshes, self.nodes = [], [], [], []
        self.mats, self.images, self.textures, self.samplers = [], [], [], [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        self.matkey, self.imgkey, self.cfgkey = {}, {}, {}

    def _view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.views.append(v)
        return len(self.views) - 1

    def acc(self, data, ctype, count, atype, target=None, minmax=None):
        a = {"bufferView": self._view(data, target), "componentType": ctype, "count": count, "type": atype}
        if minmax:
            a["min"], a["max"] = minmax
        self.accs.append(a)
        return len(self.accs) - 1

    def image(self, key, mime, data):
        if key in self.imgkey:
            return self.imgkey[key]
        nm = key if isinstance(key, str) else "_".join(str(k) for k in key)
        self.images.append({"bufferView": self._view(data), "mimeType": mime, "name": nm})
        self.textures.append({"source": len(self.images) - 1, "sampler": 0})
        self.imgkey[key] = len(self.textures) - 1
        return self.imgkey[key]

    def write(self, path):
        doc = {"asset": {"version": "2.0", "generator": "cdp2glb"},
               "scene": 0, "scenes": [{"nodes": [i for i, n in enumerate(self.nodes) if not n.pop("_child", False)]}],
               "nodes": self.nodes, "buffers": [{"byteLength": len(self.bin)}]}
        for k, v in (("bufferViews", self.views), ("accessors", self.accs), ("meshes", self.meshes),
                     ("materials", self.mats), ("images", self.images), ("textures", self.textures)):
            if v:
                doc[k] = v
        doc["samplers"] = self.samplers
        def clean(o):
            if isinstance(o, float):
                return o if o == o and abs(o) != float("inf") else 0.0
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items()}
            if isinstance(o, list):
                return [clean(v) for v in o]
            return o
        js = json.dumps(clean(doc), separators=(",", ":"), allow_nan=False).encode("ascii")
        js += b" " * (-len(js) % 4)
        binb = bytes(self.bin) + b"\0" * (-len(self.bin) % 4)
        with open(path, "wb") as f:
            f.write(struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(binb)))
            f.write(struct.pack("<I4s", len(js), b"JSON") + js)
            f.write(struct.pack("<I4s", len(binb), b"BIN\0") + binb)


def parse_texture_txt(txt):
    d = {}
    for line in txt.decode("latin1").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip().lower()] = v.strip()
    return d


def build_texture(pkg, asset, glb, texref, want_alpha, log):
    """texref like 'main1.texture' -> (glTF texture index, cfg) or None.  Never raises."""
    try:
        return _build_texture(pkg, asset, glb, texref, want_alpha, log)
    except Exception as e:
        log("   texture failed (%s): %s" % (texref, e))
        return None


def _build_texture(pkg, asset, glb, texref, want_alpha, log):
    base = texref.replace("\\", "/").split("/")[-1]
    for cand in (base + ".txt", base + ".texture.txt" if not base.endswith(".texture") else base + ".txt"):
        hit = pkg.find(asset, cand)
        if hit:
            break
    else:
        log("   texture config not found: %s" % base)
        return None
    ta, tn = hit
    cfg = parse_texture_txt(pkg.read(ta, tn))
    prim = cfg.get("primary")
    if not prim:
        return None
    key = (base, want_alpha)
    if key in glb.imgkey:
        return glb.imgkey[key], glb.cfgkey.get(key, cfg)
    h = pkg.find(ta, prim)
    if not h:
        log("   image missing: %s" % prim)
        return None
    pa, pn = h
    pdata = pkg.read(pa, pn)
    dec = decode_image(prim, pdata)
    if dec is None:  # png / jpg passthrough
        mime = "image/png" if prim.lower().endswith(".png") else "image/jpeg"
        ti = glb.image(key, mime, pdata)
        glb.cfgkey[key] = cfg
        return ti, cfg
    w, hh, rgba = dec
    alpha = cfg.get("alpha")
    if want_alpha and alpha and alpha.lower() != prim.lower():
        ah = pkg.find(ta, alpha)
        if ah:
            ad = decode_image(alpha, pkg.read(*ah))
            if ad and ad[0] == w and ad[1] == hh:
                rgba[3::4] = ad[2][0::4]
    if not want_alpha:
        rgba[3::4] = b"\xff" * (w * hh)
    if want_alpha and max(rgba[3::4]) == 0:   # 32-bit TGA whose alpha channel was never filled in
        rgba[3::4] = b"\xff" * (w * hh)
    has_a = any(c != 255 for c in rgba[3::4][:: max(1, (w * hh) // 4096)]) if want_alpha else False
    png = png_encode(w, hh, bytes(rgba), True)
    idx = glb.image(key, "image/png", png)
    cfg["_has_alpha"] = has_a
    glb.cfgkey[key] = cfg
    return idx, cfg


def rot(x, y, z):
    """Trainz/Blender (Z-up, Y-forward) -> glTF (Y-up, -Z forward)"""
    return x, z, -y


def add_mesh(pkg, asset, glb, im_name, label, log):
    prims, attach = read_im(pkg.read(asset, im_name))
    gprims = []
    tri_counts = []
    for pr in prims:
        nv, V, idx, N = pr.verts
        idx = [i for i in idx]
        idx = idx[:len(idx) // 3 * 3]
        if nv == 0 or not idx or max(idx) >= nv:
            dh, dsz = getattr(pr, "dbg", ((), 0))[:2]
            log("   skipped a broken material group (nv=%d)  GEOM header=%s size=%d" % (nv, list(dh), dsz))
            continue
        pos = bytearray()
        uv = bytearray()
        nor = bytearray()
        mn = [1e30] * 3
        mx = [-1e30] * 3
        for i in range(nv):
            x, y, z, u, v = V[i * 5:i * 5 + 5]
            if not all(c == c and abs(c) < 1e8 for c in (x, y, z, u, v)):
                x = y = z = u = v = 0.0
            gx, gy, gz = rot(x, y, z)
            pos += struct.pack("<3f", gx, gy, gz)
            uv += struct.pack("<2f", u, v)
            if gx < mn[0]: mn[0] = gx
            if gy < mn[1]: mn[1] = gy
            if gz < mn[2]: mn[2] = gz
            if gx > mx[0]: mx[0] = gx
            if gy > mx[1]: mx[1] = gy
            if gz > mx[2]: mx[2] = gz
            if N:
                nn = N[i * 3:i * 3 + 3]
                if not all(c == c and abs(c) < 1e8 for c in nn):
                    nn = (0.0, 0.0, 1.0)
                ln = (nn[0] * nn[0] + nn[1] * nn[1] + nn[2] * nn[2]) ** 0.5
                nn = (nn[0] / ln, nn[1] / ln, nn[2] / ln) if ln > 1e-8 else (0.0, 0.0, 1.0)
                nor += struct.pack("<3f", *rot(*nn))
        attrs = {"POSITION": glb.acc(bytes(pos), 5126, nv, "VEC3", 34962, ([*mn], [*mx])),
                 "TEXCOORD_0": glb.acc(bytes(uv), 5126, nv, "VEC2", 34962)}
        if N:
            attrs["NORMAL"] = glb.acc(bytes(nor), 5126, nv, "VEC3", 34962)
        ind = glb.acc(struct.pack("<%dH" % len(idx), *idx), 5123, len(idx), "SCALAR", 34963)
        # material
        mk = (pr.mat_name, pr.textures.get(1), pr.textures.get(8))
        if mk not in glb.matkey:
            m = {"name": pr.mat_name or "mat", "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.7}}
            if 1 in pr.textures:
                r = build_texture(pkg, asset, glb, pr.textures[1], True, log)
                if r:
                    ti, cfg = r
                    m["pbrMetallicRoughness"]["baseColorTexture"] = {"index": ti}
                    hint = cfg.get("alphahint", "").lower()
                    if hint == "masked":
                        m["alphaMode"] = "MASK"
                    elif hint or cfg.get("_has_alpha"):
                        m["alphaMode"] = "BLEND" if hint != "opaque" else "OPAQUE"
            if 8 in pr.textures:
                r = build_texture(pkg, asset, glb, pr.textures[8], False, log)
                if r:
                    m["normalTexture"] = {"index": r[0]}
            glb.mats.append(m)
            glb.matkey[mk] = len(glb.mats) - 1
        tri_counts.append(len(idx) // 3)
        if os.environ.get("CDP_DEBUG"):
            log("      group %-24s %s" % (pr.mat_name, (getattr(pr, "dbg", (0, 0, ""))[2:] or [""])[0]))
        gprims.append({"attributes": attrs, "indices": ind, "material": glb.matkey[mk], "mode": 4})
    if not gprims:
        return 0, 0, 0
    ntris = sum(a_ for a_ in tri_counts)
    glb.meshes.append({"name": label, "primitives": gprims})
    glb.nodes.append({"name": label, "mesh": len(glb.meshes) - 1})
    ni = len(glb.nodes) - 1
    for name, m in attach:
        t = m[9:12]
        if all(c == c and abs(c) < 1e8 for c in t):
            glb.nodes.append({"name": "%s.%s" % (label, name), "translation": list(rot(*t))})
    return ntris, len(gprims), len(attach)


def pick_meshes(pkg, asset, all_lods, keep_all=False):
    ims = [k for k in asset.files if k.endswith(".im")]
    out = []        # (label, imfile)
    covered = set()
    for k in sorted(asset.files):
        if not k.endswith(".lm.txt"):
            continue
        txt = pkg.read(asset, k).decode("latin1")
        lods = []
        for a_, b_ in re.findall(r'mesh\s*\(\s*"([\d.]+)"\s*\)\s*\{\s*name\s*=\s*"([^"]+)"', txt):
            try:
                lods.append((float(a_), b_.replace("\\", "/").split("/")[-1].lower()))
            except ValueError:
                pass
        for _, b in lods:
            covered.add(b)
        # best LOD = the biggest mesh (ties: highest LOD number)
        have = sorted([l for l in lods if l[1] in asset.files],
                      key=lambda l: (struct.unpack_from("<I", pkg.data, asset.files[l[1]].off)[0], l[0]),
                      reverse=True)
        label = k[:-len(".lm.txt")]
        if all_lods:
            for lod, b in have:
                out.append(("%s_lod%g" % (label, lod), b))
        elif have:
            out.append((label, have[0][1]))
    for im in ims:
        if im not in covered:
            out.append((im[:-3], im))
    if not keep_all:   # drop helper meshes that only clutter Blender
        out = [o for o in out if not re.search(r"shadow|collision", o[0], re.I)]
    return out


def validate_glb(path):
    """strict re-read of a written .glb; returns list of problems"""
    bad = []
    b = open(path, "rb").read()
    if b[:4] != b"glTF" or struct.unpack_from("<I", b, 8)[0] != len(b):
        bad.append("bad header/length")
        return bad
    jl = struct.unpack_from("<I", b, 12)[0]
    def nope(c):
        raise ValueError("non-standard JSON constant " + c)
    doc = json.loads(b[20:20 + jl].decode("ascii"), parse_constant=nope)
    bl = struct.unpack_from("<I", b, 20 + jl)[0]
    for i, a in enumerate(doc.get("accessors", [])):
        v = doc["bufferViews"][a["bufferView"]]
        size = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[a["type"]] * {5126: 4, 5123: 2}[a["componentType"]] * a["count"]
        if size > v["byteLength"] or v["byteOffset"] + v["byteLength"] > bl:
            bad.append("accessor %d out of range" % i)
        if v["byteLength"] == 0 or a["count"] == 0:
            bad.append("accessor %d empty" % i)
    for key in ("images", "materials", "meshes", "nodes", "textures"):
        for i, o in enumerate(doc.get(key, [])):
            if "name" in o and not isinstance(o["name"], str):
                bad.append("%s %d: name is not a string" % (key, i))
    for i, o in enumerate(doc.get("images", [])):
        if o.get("mimeType") not in ("image/png", "image/jpeg"):
            bad.append("image %d: bad mimeType" % i)
    for m in doc.get("meshes", []):
        if not m["primitives"]:
            bad.append("mesh %s has no primitives" % m.get("name"))
    return bad


def dump_tree(nodes, depth=0, maxdepth=6):
    for n in nodes:
        print("%s%s  [type %d, %d bytes]" % ("  " * depth, n.name or "<unnamed>", n.type, n.end - n.off))
        if n.type == 0 and depth < maxdepth:
            dump_tree(n.kids, depth + 1, maxdepth)


def main():
    ap = argparse.ArgumentParser(description="Convert Trainz .cdp packages to .glb")
    ap.add_argument("cdp")
    ap.add_argument("out", nargs="?", default="cdp_out")
    ap.add_argument("--list", action="store_true", help="just list assets and files")
    ap.add_argument("--tree", action="store_true", help="print the raw tag tree (for debugging)")
    ap.add_argument("--extract", action="store_true", help="also write every file of the package to disk")
    ap.add_argument("--all-lods", action="store_true", help="export every LOD level instead of the best one")
    ap.add_argument("--keep-all", action="store_true", help="also export shadow / collision helper meshes")
    ap.add_argument("--debug", action="store_true", help="print which normals / winding were chosen for every material group")
    ap.add_argument("--asset", help="only assets whose name / username contains this text")
    a = ap.parse_args()
    if a.debug:
        os.environ["CDP_DEBUG"] = "1"

    if not os.path.isfile(a.cdp):
        raise SystemExit("File not found: %s\n(put the .cdp path in quotes if it contains spaces)" % a.cdp)
    with open(a.cdp, "rb") as f:
        data = f.read()
    pkg = Package(data)
    print("%d assets" % len(pkg.assets))
    if a.tree:
        dump_tree(parse_tree(data, 16, len(data)))
        return
    if a.list:
        for s in pkg.assets:
            print("%-28s kind=%-14s %s  (%d files, %d meshes)" % (s.name, s.info.get("kind", "?"), s.info.get("username", ""), len(s.files), sum(k.endswith(".im") for k in s.files)))
        return
    os.makedirs(a.out, exist_ok=True)
    log = print
    t0, made, used = time.time(), 0, set()
    for s in pkg.assets:
        label = s.info.get("username", "") or s.name
        if a.asset and a.asset.lower() not in (s.name + " " + label).lower():
            continue
        safe = re.sub(r"[^\w.\-]+", "_", "%s_%s" % (label, s.name))[:80] or "asset"
        while safe.lower() in used:
            safe += "_"
        used.add(safe.lower())
        if a.extract:
            d = os.path.join(a.out, "files", safe)
            os.makedirs(d, exist_ok=True)
            for k in s.files:
                try:
                    open(os.path.join(d, os.path.basename(k)), "wb").write(pkg.read(s, k))
                except Exception as e:
                    print("  extract failed", k, e)
        try:
            meshes = pick_meshes(pkg, s, a.all_lods, a.keep_all)
        except Exception as e:
            print("\n== %s: could not read mesh list: %s" % (label, e))
            continue
        if not meshes:
            continue
        print("\n== %s  [%s] kind=%s" % (label, s.name, s.info.get("kind", "?")))
        glb = GLB()
        for lab, im in meshes:
            try:
                tris, nm, na = add_mesh(pkg, s, glb, im, lab, log)
                print("   %-30s %-24s %6d tris, %d material group(s), %d attach points" % (lab, im, tris, nm, na))
            except Exception as e:
                print("   FAILED %s: %s" % (im, e))
        if glb.meshes:
            path = os.path.join(a.out, safe + ".glb")
            try:
                glb.write(path)
                probs = validate_glb(path)
            except Exception as e:
                print("   FAILED writing %s: %s" % (path, e))
                continue
            made += 1
            print("   -> %s   [%s]" % (path, "valid" if not probs else "PROBLEMS: " + "; ".join(probs[:5])))
    print("\nDone: %d .glb file(s) written to %s in %.1fs" % (made, os.path.abspath(a.out), time.time() - t0))
    if not made:
        print("Nothing was exported. Try --list to see asset names, and check the --asset text.")


if __name__ == "__main__":
    main()
