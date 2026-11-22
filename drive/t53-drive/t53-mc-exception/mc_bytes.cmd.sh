python3 -I -S -B -c '
import os, re, struct, hashlib, zlib, base64, bisect, json
BLK = 65536
TRACE = None
class Elf:
 def __init__(self, path, rels):
  self.f = open(path, "rb")
  e = self.f.read(64)
  if e[:4] != b"\x7fELF" or e[4] != 2 or e[5] != 1 or struct.unpack_from("<H", e, 18)[0] != 183:
   raise SystemExit("mc_bytes result=STOP 不是 64 位小端 aarch64 ELF")
  shoff, = struct.unpack_from("<Q", e, 0x28)
  shentsize, shnum, shstrndx = struct.unpack_from("<HHH", e, 0x3A)
  self.f.seek(shoff)
  sh = self.f.read(shentsize * shnum)
  ss = [struct.unpack_from("<IIQQQQIIQQ", sh, i * shentsize) for i in range(shnum)]
  nm = self.read_at(ss[shstrndx][4], ss[shstrndx][5])
  self.sec = []
  for s in ss:
   n = nm[s[0]:nm.index(b"\0", s[0])].decode("ascii", "replace")
   self.sec.append({"name": n, "type": s[1], "flags": s[2], "addr": s[3], "off": s[4], "size": s[5], "link": s[6], "info": s[7]})
  self.by = {s["name"]: s for s in self.sec if s["name"]}
  amap = [s for s in self.sec if s["flags"] & 2 and not s["flags"] & 0x400 and s["size"] > 0]
  amap.sort(key=lambda s: s["addr"])
  self.amap = amap
  self.astart = [s["addr"] for s in amap]
  self.ends = {s["addr"] + s["size"]: s["name"] for s in amap}
  self.cnt = {"nul": 0, "tail": 0}
  self.ro_setup(rels)
 def read_at(self, off, n):
  self.f.seek(off)
  return self.f.read(n)
 def data(self, name):
  s = self.by.get(name)
  if not s or s["type"] == 8:
   return b""
  return self.read_at(s["off"], s["size"])
 def ro_setup(self, rels):
  s = self.by.get(".rodata")
  self.ro = None
  if not s:
   return
  d = self.data(".rodata")
  cut = []
  for r in rels:
   k = d.find(b"/" + r + b"\0")
   if k < 0:
    continue
   st = d.rfind(b"\0", 0, k) + 1
   cut.append((st, k))
  cut.sort()
  out = bytearray()
  cidx = [0] * (len(d) + 1)
  runs_s, runs_e = [], []
  cs = [c[0] for c in cut]
  i, n, ci = 0, len(d), 0
  while i < n:
   j = bisect.bisect_right(cs, i) - 1
   if j >= 0 and cut[j][0] <= i < cut[j][1]:
    e = cut[j][1]
    for k in range(i, e):
     cidx[k] = ci
    out += b"\x01"
    ci += 1
    i = e
    continue
   if d[i] == 0:
    e = i
    while e < n and d[e] == 0:
     e += 1
    runs_s.append(i)
    runs_e.append(e)
    for k in range(i, e):
     cidx[k] = ci
    out += b"\0"
    ci += 1
    i = e
    continue
   cidx[i] = ci
   out.append(d[i])
   ci += 1
   i += 1
  cidx[n] = ci
  self.ro = (s["addr"], n, cidx, runs_s, runs_e, cut, bytes(out))
  self.cs = cs
  self.rod = d
 def ro_token(self, off, direct):
  base, n, cidx, rs, re_, cut, _ = self.ro
  j = bisect.bisect_right(self.cs, off) - 1 if cut else -1
  if j >= 0 and cut[j][0] <= off < cut[j][1]:
   return "p%d.%d" % (cidx[off], off - cut[j][0])
  r = bisect.bisect_right(rs, off) - 1
  if r >= 0 and rs[r] <= off < re_[r]:
   self.cnt["nul"] += 1
   return "nul"
  if direct and off > 0 and self.rod[off - 1] != 0:
   e = self.rod.find(b"\0", off)
   self.cnt["tail"] += 1
   return "t" + hashlib.sha256(self.rod[off:e]).hexdigest()[:12]
  return "c%d" % cidx[off]
 def canon(self, a, direct=True):
  i = bisect.bisect_right(self.astart, a) - 1
  if i >= 0:
   s = self.amap[i]
   if a < s["addr"] + s["size"]:
    if s["name"] == ".rodata" and self.ro:
     return ".rodata:" + self.ro_token(a - s["addr"], direct)
    return "%s+%d" % (s["name"], a - s["addr"])
  if a in self.ends:
   return self.ends[a] + ":end"
  return None
def u32s(b):
 return struct.unpack("<%dI" % (len(b) // 4), b[:len(b) // 4 * 4])
def sx(v, bits):
 return v - (1 << bits) if v >> (bits - 1) & 1 else v
def imm_keep(w):
 if w & 0x9F000000 == 0x90000000:
  return w & 0x9F00001F
 if w & 0x1F000000 == 0x11000000:
  return w & 0xFFC003FF
 if w & 0x3B000000 == 0x39000000:
  return w & 0xFFC003FF
 if w & 0x3B200C00 == 0x38000000:
  return w & 0xFFE00FFF
 if w & 0x3A000000 == 0x28000000:
  return w & 0xFFC07FFF
 return w
def norm_code(E, name, st, exempt=()):
\
\
\
\
 s = E.by[name]
 words = u32s(E.data(name))
 pc0 = s["addr"]
 val = {}
 pend = {}
 slot = {}
 fwd = {}
 dead = False
 blocks = []
 cur = []
 backup = {}
 tr = TRACE.setdefault(name, []) if TRACE is not None else None
 for i, w in enumerate(words):
  pc = pc0 + 4 * i
  if i and (4 * i) % BLK == 0:
   blocks.append(hashlib.sha256("".join(cur).encode()).hexdigest()[:16])
   cur = []
  tok = None
  rd = w & 31
  rn = (w >> 5) & 31
  if dead and i not in fwd and backup:
   for r, v in backup.items():
    if v is not None:
     val[r] = v
  if dead:
   backup = {}
  if i in fwd:
   fv, fs = fwd.pop(i)
   if dead:
    val, slot = dict(fv), dict(fs)
   else:
    val = {r: v for r, v in val.items() if fv.get(r) == v}
    for k, v in fs.items():
     slot.setdefault(k, v)
   for r in list(pend):
    if r not in val:
     del pend[r]
  dead = False
  tgt = None
  if w & 0x9F000000 == 0x90000000:
   imm = sx(((w >> 29) & 3) | (((w >> 5) & 0x7FFFF) << 2), 21) << 12
   st["adrp"] += 1
   setv(val, pend, rd, (0, (pc & ~0xFFF) + imm), st)
   pend[rd] = i
   tok = "adrp%d" % rd
  elif w & 0x9F000000 == 0x10000000:
   imm = sx(((w >> 29) & 3) | (((w >> 5) & 0x7FFFF) << 2), 21)
   tok = adr_tok(E, st, w & 0x9F00001F, pc + imm, True)
   setv(val, pend, rd, (1, pc + imm))
  elif w & 0x7F800000 in (0x11000000, 0x51000000) and w >> 31 and rn in val:
   imm = ((w >> 10) & 0xFFF) << (12 if (w >> 22) & 1 else 0)
   k, base = val[rn]
   if k == 0 and (w >> 30) & 1 == 0 and not (w >> 22) & 1:
    a = base + imm
    tok = adr_tok(E, st, w & 0xFFC003FF, a, True)
   else:
    a = base - imm if (w >> 30) & 1 else base + imm
    tok = adr_tok(E, st, w & 0xFF8003FF, a, False)
   used(st, pend, rn)
   setv(val, pend, rd, (1, a))
  elif w & 0x3B000000 == 0x39000000 and rn in val:
   size, v, opc = w >> 30, (w >> 26) & 1, (w >> 22) & 3
   scale = size | ((opc & 2) << 1) if v else size
   k, base = val[rn]
   tok = adr_tok(E, st, w & 0xFFC003FF, base + (((w >> 10) & 0xFFF) << scale), k == 0)
   used(st, pend, rn)
   if not v and opc != 0 and not (size == 3 and opc == 2):
    setv(val, pend, rd, None)
  elif w & 0x3B200C00 == 0x38000000 and rn in val and val[rn][0] == 1:
   size, v, opc = w >> 30, (w >> 26) & 1, (w >> 22) & 3
   tok = adr_tok(E, st, w & 0xFFE00FFF, val[rn][1] + sx((w >> 12) & 0x1FF, 9), False)
   used(st, pend, rn)
   if not v and opc != 0 and not (size == 3 and opc == 2):
    setv(val, pend, rd, None)
  elif w & 0x3B800000 == 0x29000000 and rn in val and val[rn][0] == 1:
   opc, v, l = w >> 30, (w >> 26) & 1, (w >> 22) & 1
   scale = (2 + opc) if v else (2 + (opc >> 1))
   tok = adr_tok(E, st, w & 0xFFC07FFF, val[rn][1] + (sx((w >> 15) & 0x7F, 7) << scale), False)
   used(st, pend, rn)
   if l and not v:
    setv(val, pend, rd, None)
    setv(val, pend, (w >> 10) & 31, None)
  elif w & 0xFF800000 == 0xF9000000 and rn in (29, 31) and rn not in val:
   k = (rn, (w >> 10) & 0xFFF)
   tok = "%08x" % w
   if (w >> 22) & 1:
    setv(val, pend, rd, slot.get(k))
   else:
    if rd in val:
     used(st, pend, rd)
     slot[k] = val[rd]
    else:
     slot.pop(k, None)
  elif w & 0xFF800000 == 0xA9000000 and rn in (29, 31) and rn not in val:
   off = sx((w >> 15) & 0x7F, 7) * 8
   r2 = (w >> 10) & 31
   tok = "%08x" % w
   for r, k in ((rd, (rn, off // 8 if off >= 0 else None)), (r2, (rn, off // 8 + 1 if off >= 0 else None))):
    if k[1] is None:
     continue
    if (w >> 22) & 1:
     if 19 <= r <= 28 and r not in backup:
      backup[r] = val.get(r)
     setv(val, pend, r, slot.get(k))
    else:
     if r in val:
      used(st, pend, r)
      slot[k] = val[r]
     else:
      slot.pop(k, None)
  elif w == 0xD65F03C0 or w & 0xFFFFFC1F == 0xD61F0000:
   slot.clear()
   dead = True
   tok = "%08x" % w
  elif w & 0xFFE0FFE0 == 0xAA0003E0:
   rm = (w >> 16) & 31
   tok = "%08x" % w
   if rm in val:
    used(st, pend, rm)
    setv(val, pend, rd, val[rm])
   else:
    setv(val, pend, rd, None)
  elif w & 0x3B000000 == 0x18000000:
   tok = adr_tok(E, st, w & 0xFF00001F, pc + 4 * sx((w >> 5) & 0x7FFFF, 19), True)
   setv(val, pend, rd, None)
  elif w & 0x7C000000 == 0x14000000:
   tok = adr_tok(E, st, w & 0xFC000000, pc + 4 * sx(w & 0x3FFFFFF, 26), False)
   if w >> 31:
    calls(val, pend)
   else:
    tgt = i + sx(w & 0x3FFFFFF, 26)
    dead = True
  elif w & 0xFF000010 == 0x54000000 or w & 0x7E000000 == 0x34000000:
   tok = adr_tok(E, st, w & 0xFF00001F, pc + 4 * sx((w >> 5) & 0x7FFFF, 19), False)
   tgt = i + sx((w >> 5) & 0x7FFFF, 19)
  elif w & 0x7E000000 == 0x36000000:
   tok = adr_tok(E, st, w & 0xFFF8001F, pc + 4 * sx((w >> 5) & 0x3FFF, 14), False)
   tgt = i + sx((w >> 5) & 0x3FFF, 14)
  else:
   op0 = (w >> 25) & 15
   if op0 in (8, 9) or op0 & 7 == 5:
    setv(val, pend, rd, None)
    if rd == 31:
     slot.clear()
   elif op0 & 5 == 4 and not (w >> 26) & 1:
    if (w >> 22) & 1 or w & 0x3B000000 == 0x38000000 and (w >> 23) & 1:
     setv(val, pend, rd, None)
     if w & 0x3A000000 == 0x28000000:
      setv(val, pend, (w >> 10) & 31, None)
    if w & 0x3B000400 == 0x38000400 or w & 0x3B800000 in (0x28800000, 0x29800000):
     setv(val, pend, rn, None)
     if rn == 31:
      slot.clear()
   elif w & 0xFFF00000 == 0xD5300000:
    setv(val, pend, rd, None)
   elif w & 0xFFFFFC1F == 0xD63F0000:
    calls(val, pend)
   elif op0 & 7 == 7 and w & 0x5F20FC00 == 0x1E200000:
    setv(val, pend, rd, None)
   elif op0 & 7 == 7 and w & 0xBFE0FC00 == 0x0E003C00:
    setv(val, pend, rd, None)
   tok = "%08x" % w
  if tgt is not None and tgt > i:
   if tgt in fwd:
    fv, fs = fwd[tgt]
    fs.update({k: v for k, v in slot.items() if k not in fs})
    fwd[tgt] = ({r: v for r, v in fv.items() if val.get(r) == v}, fs)
   else:
    fwd[tgt] = (dict(val), dict(slot))
  if i in exempt:
   tok = "%08x@~" % imm_keep(w)
   st["exempt"] = st.get("exempt", 0) + 1
  cur.append(tok + ";")
  if tr is not None:
   tr.append(tok)
 blocks.append(hashlib.sha256("".join(cur).encode()).hexdigest()[:16])
 st["masked"] += st["adrp"] - st["adrp_used"]
 return blocks
def used(st, pend, r):
 if pend.pop(r, None) is not None:
  st["adrp_used"] += 1
def setv(val, pend, r, v, st=None):
 if r == 31:
  return
 if v is None:
  val.pop(r, None)
 else:
  val[r] = v
 k = pend.pop(r, None)
 if k is not None and st is not None and "unused" in st:
  st["unused"].append(k)
def calls(val, pend):
 for r in list(val):
  if r <= 18 or r == 30:
   val.pop(r, None)
   pend.pop(r, None)
def adr_tok(E, st, keep, a, direct):
 c = E.canon(a, direct)
 if c is None:
  st["masked"] += 1
  return "%08x@?" % keep
 st["mapped"] += 1
 return "%08x@%s" % (keep, c)
def rela_targets(E):
 t = {}
 for nm in (".rela.dyn", ".rela.plt"):
  d = E.data(nm)
  for i in range(0, len(d) - 23, 24):
   off, info, add = struct.unpack_from("<QQq", d, i)
   t[off] = info & 0xFFFFFFFF
 return t
def norm_rela(E, name, st):
 d = E.data(name)
 out = []
 for i in range(0, len(d) - 23, 24):
  off, info, add = struct.unpack_from("<QQq", d, i)
  typ = info & 0xFFFFFFFF
  a = E.canon(off)
  if typ in (1027, 1032):
   b = E.canon(add)
   st["mapped"] += 1
  else:
   b = str(add)
  st["mapped"] += 1
  out.append("%s|%d|%s" % (a, info, b))
 return out
def norm_ptrdata(E, name, st, rt):
 s = E.by[name]
 d = E.data(name)
 out = []
 i = 0
 while i < len(d):
  a = s["addr"] + i
  if a in rt and i + 8 <= len(d):
   v, = struct.unpack_from("<Q", d, i)
   if v:
    c = E.canon(v)
    if c is None:
     st["masked"] += 1
     out.append("?")
    else:
     st["mapped"] += 1
     out.append("@" + c)
   else:
    out.append("0")
   i += 8
  else:
   out.append("%02x" % d[i])
   i += 1
 return out
def dstr(E, off):
 d = E.dynstr
 return d[off:d.index(b"\0", off)]
def norm_dynsym(E, name, st):
 d = E.data(name)
 out = []
 for i in range(0, len(d) - 23, 24):
  n, info, other, shndx, value, size = struct.unpack_from("<IBBHQQ", d, i)
  if shndx in (0, 0xFFF1) or info & 15 == 6:
   v = str(value)
  else:
   v = E.canon(value) or "?"
   st["mapped"] += 1
  sn = E.sec[shndx]["name"] if 0 < shndx < len(E.sec) else str(shndx)
  out.append("%s|%d|%d|%s|%s|%d" % (hashlib.sha256(dstr(E, n)).hexdigest()[:12], info, other, sn, v, size))
 return out
PTR_TAGS = {3, 4, 5, 6, 7, 12, 13, 17, 23, 25, 26, 0x6ffffef5, 0x6ffffef6, 0x6ffffef7, 0x6ffffff0, 0x6ffffffe, 0x6ffffffc}
SKIP_TAGS = {0, 1, 10, 14, 15, 29}
def norm_dynamic(E, name, st):
 d = E.data(name)
 out = []
 for i in range(0, len(d) - 15, 16):
  tag, val = struct.unpack_from("<qQ", d, i)
  if tag in SKIP_TAGS:
   continue
  if tag in PTR_TAGS:
   out.append("%d=%s" % (tag, E.canon(val)))
   st["mapped"] += 1
  else:
   out.append("%d=%d" % (tag, val))
 return sorted(out)
def norm_verneed(E, name, st):
 d = E.data(name)
 out = []
 o = 0
 while o + 16 <= len(d):
  ver, cnt, fil, aux, nxt = struct.unpack_from("<HHIII", d, o)
  a = o + aux
  for _ in range(cnt):
   h, fl, oth, nm, an = struct.unpack_from("<IHHII", d, a)
   out.append("%d|%d|%d|%d|%s" % (ver, h, fl, oth, dstr(E, nm).decode("ascii", "replace")))
   if not an:
    break
   a += an
  if not nxt:
   break
  o += nxt
 return sorted(out)
def read_enc(E, d, base, o, enc):
 fmt = enc & 0x0F
 if enc == 0xFF:
  return None, 0, False
 if fmt == 0x0B:
  v, n = struct.unpack_from("<i", d, o)[0], 4
 elif fmt == 0x03:
  v, n = struct.unpack_from("<I", d, o)[0], 4
 elif fmt in (0x00, 0x04):
  v, n = struct.unpack_from("<Q", d, o)[0], 8
 elif fmt == 0x0C:
  v, n = struct.unpack_from("<q", d, o)[0], 8
 elif fmt == 0x02:
  v, n = struct.unpack_from("<H", d, o)[0], 2
 elif fmt == 0x0A:
  v, n = struct.unpack_from("<h", d, o)[0], 2
 elif fmt == 0x01:
  v, n = uleb(d, o)
  n -= o
 else:
  raise ValueError("enc %x" % enc)
 app = enc & 0x70
 if app == 0x10:
  return base + o + v, n, True
 if app == 0x30:
  return v, n, "datarel"
 if app == 0:
  return v, n, fmt in (0x00, 0x04, 0x0C) and v != 0
 raise ValueError("app %x" % enc)
def uleb(d, o):
 v = sh = 0
 while True:
  b = d[o]
  o += 1
  v |= (b & 0x7F) << sh
  sh += 7
  if not b & 0x80:
   return v, o
def sleb(d, o):
 v = sh = 0
 while True:
  b = d[o]
  o += 1
  v |= (b & 0x7F) << sh
  sh += 7
  if not b & 0x80:
   if b & 0x40:
    v -= 1 << sh
   return v, o
def ptr_tok(E, st, v, isaddr):
 if not isaddr:
  return str(v)
 c = E.canon(v)
 if c is None:
  st["masked"] += 1
  return "?"
 st["mapped"] += 1
 return "@" + c
def norm_eh_frame(E, name, st, lsdas):
 s = E.by[name]
 d = E.data(name)
 base = s["addr"]
 out = []
 cies = {}
 o = 0
 while o + 4 <= len(d):
  ln, = struct.unpack_from("<I", d, o)
  if ln == 0:
   out.append("end")
   o += 4
   continue
  st_ = o + 4
  en = st_ + ln
  cid, = struct.unpack_from("<I", d, st_)
  if cid == 0:
   p = st_ + 5
   aug = d[p:d.index(b"\0", p)]
   p += len(aug) + 1
   _, p = uleb(d, p)
   _, p = sleb(d, p)
   _, p = uleb(d, p)
   info = {"R": 0, "L": 0xFF}
   toks = ["cie", aug.decode()]
   if aug.startswith(b"z"):
    al, p = uleb(d, p)
    q = p
    for ch in aug[1:]:
     c = chr(ch)
     if c == "R":
      info["R"] = d[q]
      q += 1
     elif c == "L":
      info["L"] = d[q]
      q += 1
     elif c == "P":
      enc = d[q]
      v, n, isa = read_enc(E, d, base, q + 1, enc)
      toks.append("P" + ptr_tok(E, st, v, isa))
      q += 1 + n
    p += al
   toks.append(hashlib.sha256(d[st_ + 4:st_ + 5 + len(aug) + 1]).hexdigest()[:12])
   toks.append(hashlib.sha256(d[p:en]).hexdigest()[:12])
   cies[o] = info
   out.append("|".join(toks))
  else:
   cie = cies.get(st_ - cid)
   if cie is None:
    raise ValueError("fde without cie")
   p = st_ + 4
   v, n, isa = read_enc(E, d, base, p, cie["R"])
   toks = ["fde", ptr_tok(E, st, v, isa)]
   p += n
   rng, n2, _ = read_enc(E, d, base, p, cie["R"] & 0x0F)
   toks.append(str(rng))
   p += n2
   al, p = uleb(d, p)
   if cie["L"] != 0xFF and al:
    lv, n3, isa3 = read_enc(E, d, base, p, cie["L"])
    toks.append("L" + ptr_tok(E, st, lv, isa3))
    if lv:
     lsdas.add(lv)
   p += al
   toks.append(hashlib.sha256(d[p:en]).hexdigest()[:12])
   out.append("|".join(toks))
  o = en
 return out
def norm_eh_frame_hdr(E, name, st):
 s = E.by[name]
 d = E.data(name)
 base = s["addr"]
 out = ["v%d" % d[0]]
 p = 4
 v, n, isa = read_enc(E, d, base, p, d[1])
 out.append(ptr_tok(E, st, v, isa))
 p += n
 cnt, n, _ = read_enc(E, d, base, p, d[2] & 0x0F)
 p += n
 out.append(str(cnt))
 for i in range(cnt):
  for _ in range(2):
   x, = struct.unpack_from("<i", d, p)
   out.append(ptr_tok(E, st, base + x, True))
   p += 4
 return out
def norm_lsda(E, name, st, lsdas):
\
\
 s = E.by[name]
 d = E.data(name)
 base = s["addr"]
 toks = {}
 todo = sorted({la - base for la in lsdas if 0 <= la - base < len(d)}, reverse=True)
 seen = set()
 while todo:
  o = todo.pop()
  if o in seen:
   continue
  seen.add(o)
  try:
   end = lsda_one(E, st, d, base, o, toks)
  except (ValueError, IndexError, KeyError, struct.error):
   st["lsda_bad"] = st.get("lsda_bad", 0) + 1
   continue
  while end < len(d) and d[end] == 0:
   end += 1
  if end < len(d) and d[end] == 0xFF and end not in seen:
   todo.append(end)
 st["lsdas"] = len(seen)
 out = []
 i = 0
 while i < len(d):
  if i in toks:
   n, tk = toks[i]
   out.append(tk)
   i += n
  else:
   out.append("%02x" % d[i])
   i += 1
 return out
def lsda_one(E, st, d, base, o, toks):
 p = o
 lpenc = d[p]
 p += 1
 if lpenc != 0xFF:
  _, n, _ = read_enc(E, d, base, p, lpenc)
  p += n
 ttenc = d[p]
 p += 1
 ttbase = None
 if ttenc != 0xFF:
  tto, p2 = uleb(d, p)
  ttbase = p2 + tto
  p = p2
 csenc = d[p]
 p += 1
 cslen, p = uleb(d, p)
 cend = p + cslen
 maxa = 0
 aend = cend
 while p < cend:
  for _ in range(3):
   _, n, _ = read_enc(E, d, base, p, csenc & 0x0F)
   p += n
  act, p = uleb(d, p)
  if act:
   q = cend + act - 1
   for _ in range(10000):
    flt, q2 = sleb(d, q)
    nx, q3 = sleb(d, q2)
    aend = max(aend, q3)
    if flt > maxa:
     maxa = flt
    if not nx:
     break
    q = q2 + nx
 if ttbase is not None:
  sz = {0x0B: 4, 0x03: 4, 0x00: 8, 0x04: 8, 0x0C: 8}[ttenc & 0x0F]
  k = 1
  while ttbase - k * sz >= aend:
   q = ttbase - k * sz
   v, n, isa = read_enc(E, d, base, q, ttenc & 0x7F)
   if k > maxa and not (v and (E.canon(v) or "").split("+")[0] in (".got", ".data.rel.ro", ".data", ".bss")):
    break
   toks[q] = (n, ptr_tok(E, st, v, isa) if v else "0")
   k += 1
 return ttbase if ttbase is not None else aend
def norm_notes(E, name, st):
 d = E.data(name)
 out = []
 o = 0
 while o + 12 <= len(d):
  nsz, dsz, typ = struct.unpack_from("<III", d, o)
  q = o + 12 + (nsz + 3) // 4 * 4
  out.append("%d|%s" % (typ, d[o + 12:o + 12 + nsz].hex()))
  if typ in (0x100, 0x101) and dsz in (8, 16):
   for v in struct.unpack_from("<%dQ" % (dsz // 8), d, q):
    out.append(ptr_tok(E, st, v, True))
  else:
   out.append(d[q:q + dsz].hex())
  o = q + (dsz + 3) // 4 * 4
 return out
def norm_all(path, rels, exempt=()):
 E = Elf(path, rels)
 E.dynstr = E.data(".dynstr")
 res = {}
 lsdas = set()
 rt = rela_targets(E)
 order = [s["name"] for s in E.sec if s["name"]]
 for n in order:
  s = E.by[n]
  raw = hashlib.sha256(E.data(n)).hexdigest()[:16] if s["type"] != 8 else "nobits"
  st = {"mapped": 0, "masked": 0, "adrp": 0, "adrp_used": 0}
  blocks = None
  how = "raw"
  if n in (".text", ".plt", ".init", ".fini"):
   blocks = norm_code(E, n, st, exempt if n == ".text" else ())
   norm = hashlib.sha256("".join(blocks).encode()).hexdigest()[:16]
   how = "code"
  elif n in (".rela.dyn", ".rela.plt"):
   norm, how = H(norm_rela(E, n, st), n), "rela"
  elif n in (".init_array", ".fini_array", ".data.rel.ro", ".got", ".got.plt", ".data"):
   norm, how = H(norm_ptrdata(E, n, st, rt), n), "ptr"
  elif n == ".dynsym":
   norm, how = H(norm_dynsym(E, n, st), n), "dynsym"
  elif n == ".dynamic":
   norm, how = H(norm_dynamic(E, n, st), n), "dynamic"
  elif n == ".gnu.version_r":
   norm, how = H(norm_verneed(E, n, st), n), "verneed"
  elif n == ".eh_frame":
   norm, how = H(norm_eh_frame(E, n, st, lsdas), n), "eh"
  elif n == ".eh_frame_hdr":
   norm, how = H(norm_eh_frame_hdr(E, n, st), n), "ehhdr"
  elif n == ".gnu.build.attributes":
   norm, how = H(norm_notes(E, n, st), n), "notes"
  elif n == ".rodata":
   ro = E.ro[6]
   blocks = [hashlib.sha256(ro[i:i + BLK]).hexdigest()[:16] for i in range(0, max(len(ro), 1), BLK)]
   norm, how = hashlib.sha256(ro).hexdigest()[:16], "rodata"
   st["masked"] = len(E.ro[5])
   st["nulruns"] = len(E.ro[3])
  else:
   norm = raw
  res[n] = {"raw": raw, "norm": norm, "how": how, "size": s["size"], "type": s["type"], "flags": s["flags"],
    "blocks": blocks, "exempt": st.get("exempt", 0),
    "mapped": st["mapped"], "masked": st["masked"], "adrp": st["adrp"],
    "nulruns": st.get("nulruns", 0)}
 if ".gcc_except_table" in E.by:
  st = {"mapped": 0, "masked": 0}
  res[".gcc_except_table"].update(norm=H(norm_lsda(E, ".gcc_except_table", st, lsdas), ".gcc_except_table"), how="lsda",
         mapped=st["mapped"], masked=st["masked"])
 res["@path_cuts"] = len(E.ro[5]) if E.ro else 0
 res["@nulruns"] = len(E.ro[3]) if E.ro else 0
 res["@nul_refs"] = E.cnt["nul"]
 res["@tail_refs"] = E.cnt["tail"]
 return res
def H(lst, name=None):
 if TRACE is not None and name:
  TRACE[name] = lst
 return hashlib.sha256("\n".join(lst).encode()).hexdigest()[:16]

LAB_DATA = """
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
9yK+ItnJUqhYvkacqWHi5oy7Sd/89d//AeeHhXI=
"""
STORE = "libmooncake_store.so"
SKIP = {".dynstr": "patchelf", ".note.gnu.build-id": "build_id", ".comment": "mc_comment"}
def pick():
 me = os.getpid()
 want = os.environ.get("PID", "").strip()
 if want and not want.isdigit():
  raise SystemExit("mc_bytes result=STOP PID=%s 不是进程号" % want[:20])
 pids = [int(want)] if want else sorted((int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me), reverse=True)
 for p in pids:
  try:
   cmd = open("/proc/%d/cmdline" % p, "rb").read(4096)
   if not want and b"vl" b"lm" not in cmd.lower():
    continue
   for l in open("/proc/%d/maps" % p):
    f = l.split(None, 5)
    if len(f) == 6 and f[5].strip().startswith("/") and os.path.basename(f[5].strip()) == STORE:
     return p, f[5].strip(), bool(want)
  except OSError:
   continue
 raise SystemExit("mc_bytes result=STOP " + ("进程 %s 不存在或没有映射 %s" % (want, STORE) if want else "没有找到映射 %s 的 vLLM 进程（vLLM 没在运行，或者没加载 Mooncake）" % STORE))
def main():
 try:
  os.nice(19)
 except OSError:
  pass
 pid, path, given = pick()
 lab = json.loads(zlib.decompress(base64.b64decode("".join(LAB_DATA.split()))))
 h = hashlib.sha256()
 with open(path, "rb") as fh:
  for b in iter(lambda: fh.read(1 << 20), b""):
   h.update(b)
  size = fh.tell()
 res = norm_all(path, [r.encode() for r in lab["rels"]], set(lab["ex"]))
 print("mc_bytes pid=%d%s file=%s/%s size=%d sha16=%s sections=%d(lab %d)"
  % (pid, "(PID=)" if given else "", "mooncake" if os.path.basename(os.path.dirname(path)) == "mooncake" else "<其他目录>",
   STORE, size, h.hexdigest()[:16], len([n for n in res if not n.startswith("@")]), len(lab["sec"])))
 names = [n for n in res if not n.startswith("@")] + [n for n in lab["sec"] if n not in res]
 bad, rawdiff, masked = [], 0, 0
 for n in names:
  a, b = res.get(n), lab["sec"].get(n)
  if b is None or a is None:
   s = a or {"type": b[3], "size": b[2]}
   print("mc_bytes sec=%s only=%s type=%d size=%d" % (n, "site" if b is None else "lab", s["type"], s["size"]))
   bad.append(n)
   continue
  raw = "same" if a["raw"] == b[0] else "differs"
  if n in SKIP:
   print("mc_bytes sec=%s skip=%s raw=%s" % (n, SKIP[n], raw))
   continue
  if (a["size"] != b[2] and n != ".rodata") or a["type"] != b[3]:
   print("mc_bytes sec=%s size=%d(lab %d) type=%d(lab %d) norm=differs" % (n, a["size"], b[2], a["type"], b[3]))
   bad.append(n)
   continue
  rawdiff += raw == "differs"
  norm = "same" if a["norm"] == b[1] else "differs"
  line = "mc_bytes sec=%s size=%s raw=%s norm=%s" % (n, "same" if a["size"] == b[2] else "%+d" % (a["size"] - b[2]), raw, norm)
  lb = lab["tb"] if n == ".text" else lab["rb"] if n == ".rodata" else None
  if lb is not None and a["blocks"] is not None:
   d = [str(i) for i, (x, y) in enumerate(zip(a["blocks"], lb)) if x[:12] != y]
   line += " blocks=%d differ_blocks=%s" % (len(lb), ",".join(d[:20]) + (",…" if len(d) > 20 else "") if d else "-")
  line += " mapped=%d" % a["mapped"]
  if n == ".text":
   line += " masked=%d(adrp %d, lab_known %d)" % (a["masked"] + a["exempt"], a["masked"], a["exempt"])
   masked += a["masked"] + a["exempt"]
  elif n == ".rodata":
   line += " masked=path_prefix %d, nul_runs %d" % (res["@path_cuts"], res["@nulruns"])
  elif a["masked"]:
   line += " masked=%d" % a["masked"]
   masked += a["masked"]
  print(line)
  if norm == "differs":
   bad.append(n)
 print("mc_bytes refs .rodata: by_content tail_strings=%d zero_or_empty=%d" % (res["@tail_refs"], res["@nul_refs"]))
 if bad:
  print("mc_bytes result=differs sections=%s" % ",".join(bad[:20]))
 elif not rawdiff:
  print("mc_bytes result=raw_same")
 else:
  print("mc_bytes result=same_after_normalization masked_words=%d path_prefixes=%d" % (masked, res["@path_cuts"]))
main()
'
