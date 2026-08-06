#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# montar.py — Montador de legendas para FMV STR do PS1 (Rurouni Kenshin)
# Python puro, sem dependencias. Feito para a-Shell (iOS).
#
# Uso:
#   python3 tools/montar.py extract/RU01.MOV videos/RU01.json -o zroup/mod/RU01.MOV
#   Opcoes:
#     --so-validar     apenas simula os custos, nao gera arquivo
#     --preview N      exporta PNG do quadro N legendado (confira a legibilidade)
#
# Garantias de projeto (verificadas ao final de toda montagem):
#   1. Setores de audio byte a byte IDENTICOS ao original
#   2. Quadros sem legenda byte a byte IDENTICOS ao original
#      (re-codificacao sem perdas provada: mesmos simbolos -> mesmos bytes)
#   3. Quadros legendados: apenas os macroblocos sob o texto sao alterados;
#      todo o resto do quadro carrega os coeficientes originais intactos
#   4. Nenhum quadro excede sua capacidade de setores (nada de piscar/crash)
#   5. Se qualquer verificacao falhar, o arquivo NAO e entregue

import sys, os, json, struct, math, zlib, base64, hashlib

SEC = 2048
MAGIC = b'\x60\x01\x01\x80'

# ============================================================ tabela VLC
AC = {
 '11':(0,1),'011':(1,1),'0100':(0,2),'0101':(2,1),
 '00101':(0,3),'00110':(4,1),'00111':(3,1),
 '000100':(7,1),'000101':(6,1),'000110':(1,2),'000111':(5,1),
 '0000100':(2,2),'0000101':(9,1),'0000110':(0,4),'0000111':(8,1),
 '00100000':(13,1),'00100001':(0,6),'00100010':(12,1),'00100011':(11,1),
 '00100100':(3,2),'00100101':(1,3),'00100110':(0,5),'00100111':(10,1),
 '0000001000':(16,1),'0000001001':(5,2),'0000001010':(0,7),'0000001011':(2,3),
 '0000001100':(1,4),'0000001101':(15,1),'0000001110':(14,1),'0000001111':(4,2),
 '000000010000':(0,11),'000000010001':(8,2),'000000010010':(4,3),'000000010011':(0,10),
 '000000010100':(2,4),'000000010101':(7,2),'000000010110':(21,1),'000000010111':(20,1),
 '000000011000':(0,9),'000000011001':(19,1),'000000011010':(18,1),'000000011011':(1,5),
 '000000011100':(3,3),'000000011101':(0,8),'000000011110':(6,2),'000000011111':(17,1),
 '0000000010000':(10,2),'0000000010001':(9,2),'0000000010010':(5,3),'0000000010011':(3,4),
 '0000000010100':(2,5),'0000000010101':(1,7),'0000000010110':(1,6),'0000000010111':(0,15),
 '0000000011000':(0,14),'0000000011001':(0,13),'0000000011010':(0,12),'0000000011011':(26,1),
 '0000000011100':(25,1),'0000000011101':(24,1),'0000000011110':(23,1),'0000000011111':(22,1),
}
_v = list(range(31,15,-1))
for _i,_s in enumerate(range(16,32)):
    AC['000000000'+format(_s,'05b')] = (0,_v[_i])
_q15=[(1,8),(1,9),(1,10),(1,11),(1,12),(1,13),(1,14),(1,15),(1,16),(1,17),(1,18),(6,3),(11,2),(12,2),(13,2),(14,2)]
for _s,_rl in zip(range(31,15,-1),_q15):
    AC['0000000000'+format(_s,'05b')] = _rl
_q16=[(27,1),(28,1),(29,1),(30,1),(31,1),(0,40),(0,39),(0,38),(0,37),(0,36),(0,35),(0,34),(0,33),(0,32),(2,6),(16,2)]
for _s,_rl in zip(range(31,15,-1),_q16):
    AC['00000000000'+format(_s,'05b')] = _rl
MAXLEN = max(map(len, AC))
ENC = {v:k for k,v in AC.items()}

ZIG = [0,1,8,16,9,2,3,10,17,24,32,25,18,11,4,5,12,19,26,33,40,48,41,34,
       27,20,13,6,7,14,21,28,35,42,49,56,57,50,43,36,29,22,15,23,30,37,
       44,51,58,59,52,45,38,31,39,46,53,60,61,54,47,55,62,63]
QT = [2,16,19,22,26,27,29,34,16,16,22,24,27,29,34,37,19,22,26,27,29,34,
      34,38,22,22,26,27,29,34,37,40,22,26,27,29,32,35,40,48,26,27,29,32,
      35,40,48,58,26,27,29,34,38,46,56,69,27,29,35,38,46,56,69,83]

# ============================================================ bits
class BR:
    __slots__ = ('w','p')
    def __init__(s, b):
        s.w = struct.unpack('<%dH' % (len(b)//2), b[:len(b)//2*2]); s.p = 0
    def bit(s):
        v = (s.w[s.p >> 4] >> (15-(s.p & 15))) & 1; s.p += 1; return v
    def bits(s, n):
        v = 0
        for _ in range(n): v = (v << 1) | s.bit()
        return v

class BW:
    __slots__ = ('parts',)
    def __init__(s): s.parts = []
    def put(s, t): s.parts.append(t)
    def putn(s, v, n): s.parts.append(format(v & ((1 << n)-1), '0%db' % n))
    def close(s):
        bits = ''.join(s.parts) + '0111111111'          # codigo de fim 0x1FF
        pad = (-len(bits)) % 32
        if pad == 0: pad = 32                            # borda: original acrescenta 4B
        bits += '0'*pad
        out = bytearray()
        for i in range(0, len(bits), 16):
            out += struct.pack('<H', int(bits[i:i+16], 2))
        return bytes(out)

# ============================================================ STR
class Str:
    def __init__(self, caminho):
        self.data = bytearray(open(caminho, 'rb').read())
        self.frames = {}            # n -> dict(chunks, fsz, setores[], w, h)
        self.audio_pos = []         # indices de setores nao-video
        for si in range(len(self.data)//SEC):
            c = self.data[si*SEC:(si+1)*SEC]
            if c[:4] == MAGIC:
                cn, ch = struct.unpack('<HH', c[4:8])
                fn, = struct.unpack('<I', c[8:12])
                fsz, = struct.unpack('<I', c[12:16])
                w, h = struct.unpack('<HH', c[16:20])
                f = self.frames.setdefault(fn, {'chunks':ch,'fsz':fsz,'w':w,'h':h,'set':{}})
                f['set'][cn] = si
            else:
                self.audio_pos.append(si)
        anyf = next(iter(self.frames.values()))
        self.W, self.H = anyf['w'], anyf['h']
    def blob(self, fn):
        f = self.frames[fn]
        return b''.join(bytes(self.data[si*SEC+32:(si+1)*SEC])
                        for _, si in sorted(f['set'].items()))
    def gravar_frame(self, fn, novo):
        """Escreve o novo bitstream nos setores do quadro, preservando headers
        e atualizando o campo fsz. Preenche o resto com zeros."""
        f = self.frames[fn]
        cap = f['chunks']*2016
        assert len(novo) <= cap, (fn, len(novo), cap)
        buf = novo + b'\x00'*(cap-len(novo))
        for cn, si in sorted(f['set'].items()):
            off = si*SEC
            struct.pack_into('<I', self.data, off+12, len(novo))
            self.data[off+32:off+2048] = buf[cn*2016:(cn+1)*2016]
        f['fsz'] = len(novo)

# ============================================================ decodificacao
def decode_syms(blob, W, H):
    """bitstream -> (qscale, versao, lista de blocos [dc, (run,lvl[,E])...])"""
    n_half, magic, qs, ver = struct.unpack('<HHHH', blob[:8])
    if magic != 0x3800:
        raise ValueError('magic invalido %04x' % magic)
    br = BR(blob[8:])
    out = []
    for _ in range((W//16)*(H//16)*6):
        dc = br.bits(10)
        coefs = [dc if dc < 512 else dc-1024]
        while True:
            s = ''
            fim = False
            while True:
                s += '1' if br.bit() else '0'
                if s == '10': fim = True; break
                if s in AC:
                    run, lvl = AC[s]
                    if br.bit(): lvl = -lvl
                    coefs.append((run, lvl)); break
                if s == '000001':
                    run = br.bits(6); lvl = br.bits(10)
                    if lvl >= 512: lvl -= 1024
                    coefs.append((run, lvl, 'E')); break
                if len(s) > MAXLEN:
                    raise ValueError('VLC desconhecido '+s)
            if fim: break
        out.append(coefs)
    return qs, ver, out

def encode_syms(blocks, qs, ver):
    bw = BW(); codes = 0
    for coefs in blocks:
        bw.putn(coefs[0], 10); codes += 1
        for c in coefs[1:]:
            run, lvl = c[0], c[1]
            if len(c) == 3:
                bw.put('000001'); bw.putn(run, 6); bw.putn(lvl, 10)
            else:
                key = (run, abs(lvl))
                pre = ENC.get(key)
                if pre:
                    bw.put(pre); bw.put('1' if lvl < 0 else '0')
                else:
                    bw.put('000001'); bw.putn(run, 6); bw.putn(lvl, 10)
            codes += 1
        bw.put('10'); codes += 1
    body = bw.close()
    n_half = -(-((codes+1)//2)//32)*32
    return struct.pack('<HHHH', n_half, 0x3800, qs, ver) + body

def bits_bloco(coefs):
    """custo em bits de um bloco codificado (DC + ACs + EOB)"""
    n = 10
    for c in coefs[1:]:
        if len(c) == 3:
            n += 22
        else:
            pre = ENC.get((c[0], abs(c[1])))
            n += (len(pre)+1) if pre else 22
    return n + 2


def syms_para_coefs(coefs, qs):
    """simbolos de um bloco -> matriz 8x8 de coeficientes dequantizados"""
    out = [0.0]*64
    out[0] = coefs[0]*QT[0]
    idx = 0
    for c in coefs[1:]:
        idx += c[0]+1
        if idx < 64:
            out[ZIG[idx]] = (c[1]*QT[ZIG[idx]]*qs)/8.0
    return out

# ============================================================ DCT
_C = [1/math.sqrt(2)]+[1.0]*7
_COS = [[math.cos((2*x+1)*u*math.pi/16) for u in range(8)] for x in range(8)]

def idct(F):
    tmp = [0.0]*64; out = [0.0]*64
    for y in range(8):
        b = y*8
        for x in range(8):
            s = 0.0
            for u in range(8):
                s += _C[u]*F[b+u]*_COS[x][u]
            tmp[b+x] = s/2
    for x in range(8):
        for y in range(8):
            s = 0.0
            for v in range(8):
                s += _C[v]*tmp[v*8+x]*_COS[y][v]
            out[y*8+x] = s/2
    return out

def fdct(px):
    tmp = [0.0]*64; out = [0.0]*64
    for y in range(8):
        b = y*8
        for u in range(8):
            s = 0.0
            for x in range(8):
                s += px[b+x]*_COS[x][u]
            tmp[b+u] = s*_C[u]/2
    for u in range(8):
        for v in range(8):
            s = 0.0
            for y in range(8):
                s += tmp[y*8+u]*_COS[y][v]
            out[v*8+u] = s*_C[v]/2
    return out

def coefs_para_syms(F, qs, corte=64):
    """matriz de coeficientes -> simbolos quantizados (com corte de squash)"""
    dc = int(round(F[0]/QT[0]))
    dc = max(-512, min(511, dc))
    syms = [dc]
    run = 0
    for i in range(1, 64):
        if i >= corte:
            break
        z = ZIG[i]
        lvl = int(round(F[z]*8.0/(QT[z]*qs)))
        if lvl == 0:
            run += 1; continue
        lvl = max(-511, min(511, lvl))
        syms.append((run, lvl))
        run = 0
    return syms

def _cortar(coefs, corte):
    """mantém apenas ACs com índice zigzag < corte (re-agrupando runs)"""
    out = []
    idx = 0
    run_extra = 0
    for c in coefs[1:]:
        idx += c[0]+1
        if idx < corte:
            out.append((idx, (c[0]+run_extra, c[1]) if len(c) == 2
                        else (c[0]+run_extra, c[1], 'E')))
            run_extra = 0
        else:
            run_extra += c[0]+1
    return out


# ============================================================ fonte embutida
FONTDATA = "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"
_fontes = None
def fonte(tam):
    global _fontes
    if _fontes is None:
        _fontes = json.loads(zlib.decompress(base64.b64decode(FONTDATA)).decode())
    tams = sorted(int(k) for k in _fontes)
    t = min(tams, key=lambda x: abs(x-tam))
    return _fontes[str(t)]

def render_texto(texto, tam, contorno):
    """Renderiza texto (linhas separadas por \\n) em duas mascaras 0..255:
    (preenchimento, contorno). Retorna (largura, altura, fill, outline)."""
    ft = fonte(tam)
    lh = ft['lh']; G = ft['g']
    linhas = texto.split('\n')
    esp = 2  # espaco entre linhas
    larguras = []
    for ln in linhas:
        w = 0
        for ch in ln:
            g = G.get(ch) or G.get('?')
            w += g[0]
        larguras.append(w)
    W = max(larguras) if larguras else 0
    H = lh*len(linhas) + esp*(len(linhas)-1)
    m = contorno
    FW, FH = W + 2*m, H + 2*m
    fill = bytearray(FW*FH)
    y = m
    for li, ln in enumerate(linhas):
        x = m + (W - larguras[li])//2
        for ch in ln:
            g = G.get(ch) or G.get('?')
            adv, gw, gh, oy, b64 = g
            if gw:
                raw = base64.b64decode(b64)
                for yy in range(gh):
                    base = (y+oy+yy)*FW + x
                    row = raw[yy*gw:(yy+1)*gw]
                    for xx in range(gw):
                        v = row[xx]
                        if v:
                            p = base+xx
                            if fill[p] < v: fill[p] = v
            x += adv
        y += lh + esp
    # contorno = dilatacao chebyshev da mascara de preenchimento
    outline = bytearray(FW*FH)
    if m > 0:
        for yy in range(FH):
            for xx in range(FW):
                v = fill[yy*FW+xx]
                if v > 40:
                    for dy in range(-m, m+1):
                        ny = yy+dy
                        if 0 <= ny < FH:
                            base = ny*FW
                            for dx in range(-m, m+1):
                                nx = xx+dx
                                if 0 <= nx < FW:
                                    p = base+nx
                                    if outline[p] < v: outline[p] = v
    return FW, FH, fill, outline

# ============================================================ legenda em pixels
Y_TEXTO   = 110.0    # luma do texto (plano Y centrado em 0; ~238 em 0-255)
Y_BORDA   = -104.0   # luma do contorno (~24 em 0-255)

def macroblocos_afetados(x0, y0, w, h, W, H):
    mbs = set()
    for my in range(max(0, y0)//16, min(H, y0+h+15)//16):
        for mx in range(max(0, x0)//16, min(W, x0+w+15)//16):
            mbs.add((mx, my))
    return mbs

def aplicar_legenda(blocks, qs, W, H, texto, tam, contorno, margem, corte=64):
    """Decodifica apenas os macroblocos sob o texto, desenha, recodifica.
    Retorna nova lista de blocos (splice em dominio de coeficientes)."""
    FW, FH, fill, outline = render_texto(texto, tam, contorno)
    x0 = (W - FW)//2
    y0 = H - margem - FH
    mbw, mbh = W//16, H//16
    afetados = macroblocos_afetados(x0, y0, FW, FH, W, H)

    novos = list(blocks)
    for (mx, my) in afetados:
        bi = (mx*mbh + my)*6          # ordem coluna-major, 6 blocos por MB
        # decodificar blocos deste MB para pixels
        cr = idct(syms_para_coefs(blocks[bi+0], qs))
        cb = idct(syms_para_coefs(blocks[bi+1], qs))
        ys = [idct(syms_para_coefs(blocks[bi+2+k], qs)) for k in range(4)]
        # compor texto sobre o luma
        for py in range(16):
            gy = my*16 + py - y0
            if not (0 <= gy < FH): continue
            for px in range(16):
                gx = mx*16 + px - x0
                if not (0 <= gx < FW): continue
                a_f = fill[gy*FW+gx]/255.0
                a_o = outline[gy*FW+gx]/255.0
                if a_f == 0.0 and a_o == 0.0: continue
                blk = ys[(py//8)*2 + (px//8)]
                i = (py % 8)*8 + (px % 8)
                v = blk[i]
                if a_o > 0.0:
                    v = v*(1-a_o) + Y_BORDA*a_o
                if a_f > 0.0:
                    v = v*(1-a_f) + Y_TEXTO*a_f
                blk[i] = v
                # neutralizar croma sob o texto (mesma alpha)
                a = max(a_f, a_o)
                ci = ((my*16+py)//2 - my*8)*8 + ((mx*16+px)//2 - mx*8)
                cr[ci] *= (1-a); cb[ci] *= (1-a)
        # recodificar os 6 blocos
        novos[bi+0] = coefs_para_syms(fdct(cr), qs, corte)
        novos[bi+1] = coefs_para_syms(fdct(cb), qs, corte)
        for k in range(4):
            novos[bi+2+k] = coefs_para_syms(fdct(ys[k]), qs, corte)
    return novos

# ============================================================ PNG puro (preview)
def salvar_png(caminho, W, H, rgb):
    def chunk(tipo, dados):
        c = tipo + dados
        return struct.pack('>I', len(dados)) + c + struct.pack('>I', zlib.crc32(c))
    raw = b''.join(b'\x00' + bytes(rgb[y*W*3:(y+1)*W*3]) for y in range(H))
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 9))
           + chunk(b'IEND', b''))
    open(caminho, 'wb').write(png)

def frame_para_rgb(blocks, qs, W, H):
    mbh = H//16
    Y  = [[0.0]*W for _ in range(H)]
    Cb = [[0.0]*(W//2) for _ in range(H//2)]
    Cr = [[0.0]*(W//2) for _ in range(H//2)]
    for mbx in range(W//16):
        for mby in range(mbh):
            bi = (mbx*mbh + mby)*6
            cr = idct(syms_para_coefs(blocks[bi+0], qs))
            cb = idct(syms_para_coefs(blocks[bi+1], qs))
            for k in range(4):
                blk = idct(syms_para_coefs(blocks[bi+2+k], qs))
                ox, oy = mbx*16+(k % 2)*8, mby*16+(k//2)*8
                for yy in range(8):
                    for xx in range(8):
                        Y[oy+yy][ox+xx] = blk[yy*8+xx]
            for yy in range(8):
                for xx in range(8):
                    Cr[mby*8+yy][mbx*8+xx] = cr[yy*8+xx]
                    Cb[mby*8+yy][mbx*8+xx] = cb[yy*8+xx]
    rgb = bytearray(W*H*3)
    for y in range(H):
        for x in range(W):
            l = Y[y][x]; cb = Cb[y//2][x//2]; cr = Cr[y//2][x//2]
            r = l + 1.402*cr; g = l - 0.3437*cb - 0.7143*cr; b = l + 1.772*cb
            p = (y*W+x)*3
            rgb[p]   = max(0, min(255, int(r+128.5)))
            rgb[p+1] = max(0, min(255, int(g+128.5)))
            rgb[p+2] = max(0, min(255, int(b+128.5)))
    return rgb

# ============================================================ montagem
def montar(video_path, json_path, saida, so_validar=False, preview=None):
    cfg = json.load(open(json_path, encoding='utf-8'))
    pad = cfg.get('padrao', {})
    p_fonte    = pad.get('fonte', 13)
    p_contorno = pad.get('contorno', 1)
    p_margem   = pad.get('margem', 8)

    falas = sorted(cfg['falas'], key=lambda f: f['quadro_ini'])
    for a, b in zip(falas, falas[1:]):
        if b['quadro_ini'] <= a['quadro_fim']:
            print('ERRO: falas %s e %s se sobrepoem' %
                  (a.get('id','?'), b.get('id','?')))
            sys.exit(1)

    st = Str(video_path)
    orig_sha = hashlib.sha256(bytes(st.data)).hexdigest()
    W, H = st.W, st.H
    print('Video: %s  %dx%d  %d quadros  SHA-256 %s...' %
          (os.path.basename(video_path), W, H, len(st.frames), orig_sha[:16]))

    # ---------- PASSADA 1: encontrar corte (squash) unico por fala ----------
    plano = []      # (fala, corte_final)
    for fl in falas:
        tam  = fl.get('fonte', p_fonte)
        ctn  = fl.get('contorno', p_contorno)
        mrg  = fl.get('margem', p_margem)
        txt  = fl['texto']
        fid  = fl.get('id', '?')
        pior_corte = 64
        detalhe = []
        for fn in range(fl['quadro_ini'], fl['quadro_fim']+1):
            if fn not in st.frames: continue
            f = st.frames[fn]
            cap = f['chunks']*2016
            blob = st.blob(fn)
            qs, ver, blocks = decode_syms(blob, W, H)
            # custo fixo: bits de todos os blocos, menos os que serão editados
            mbh = H//16
            FW, FH, _f, _o = render_texto(txt, tam, ctn)
            afet = macroblocos_afetados((W-FW)//2, H-mrg-FH, FW, FH, W, H)
            idx_edit = set()
            for (mx, my) in afet:
                for k in range(6):
                    idx_edit.add((mx*mbh+my)*6+k)
            bits_fixos = sum(bits_bloco(b) for i, b in enumerate(blocks)
                             if i not in idx_edit)
            # editar uma única vez sem corte; depois só recortar os símbolos
            cheio = aplicar_legenda(blocks, qs, W, H, txt, tam, ctn, mrg, 64)
            def cabe(corte):
                tot = bits_fixos
                for i in idx_edit:
                    b = cheio[i]
                    if corte < 64:
                        b = [b[0]] + [c for j, c in _cortar(b, corte)]
                    tot += bits_bloco(b)
                # fim de quadro + alinhamento + cabeçalho, com folga p/ borda
                return (tot + 10 + 63)//8 + 8 + 4 <= cap
            corte = 64
            while corte >= 1 and not cabe(corte):
                corte = corte - 4 if corte > 8 else corte - 1
            if corte < 1:
                print('ERRO: fala #%s nao cabe no quadro %d nem com squash maximo.' % (fid, fn))
                print('      Sugestoes: contorno menor, fonte menor, margem +4/+8,')
                print('      ou deslocar quadro_ini em 1 (fugir de quadro de 8 chunks).')
                sys.exit(1)
            enc_len = None  # tamanho real só é conhecido na passada 2
            detalhe.append((fn, cap, cap, corte))
            if corte < pior_corte: pior_corte = corte
        plano.append((fl, pior_corte, detalhe))
        print('  fala #%-3s q%d-%d  fonte=%d ct=%d mg=%d  corte=%d'
              % (fid, fl['quadro_ini'], fl['quadro_fim'], tam, ctn, mrg,
                 pior_corte))

    if so_validar:
        print('\n--so-validar: nenhuma alteracao gravada.')
        return

    # ---------- PASSADA 2: aplicar com corte uniforme (legenda estavel) ----------
    editados = {}
    folgas = {}
    for fl, corte, _ in plano:
        tam = fl.get('fonte', p_fonte)
        ctn = fl.get('contorno', p_contorno)
        mrg = fl.get('margem', p_margem)
        for fn in range(fl['quadro_ini'], fl['quadro_fim']+1):
            if fn not in st.frames: continue
            f = st.frames[fn]
            cap = f['chunks']*2016
            blob = st.blob(fn)
            qs, ver, blocks = decode_syms(blob, W, H)
            novos = aplicar_legenda(blocks, qs, W, H, fl['texto'], tam, ctn, mrg, corte)
            enc = encode_syms(novos, qs, ver)
            assert len(enc) <= cap, (fn, len(enc), cap)
            st.gravar_frame(fn, enc)
            editados[fn] = fl.get('id', '?')
            folgas.setdefault(fl.get('id', '?'), []).append(cap-len(enc))
            if preview == fn:
                png = os.path.splitext(saida)[0] + ('_preview_q%d.png' % fn)
                salvar_png(png, W, H, frame_para_rgb(novos, qs, W, H))
                print('  preview: %s' % png)

    for fid, fs in folgas.items():
        print('  fala #%-3s folga real minima: %dB' % (fid, min(fs)))

    # ---------- VERIFICACAO FINAL (obrigatoria) ----------
    print('\nVerificacao final:')
    original = open(video_path, 'rb').read()
    novo = bytes(st.data)
    # 1. audio intacto
    audio_ok = all(original[si*SEC:(si+1)*SEC] == novo[si*SEC:(si+1)*SEC]
                   for si in st.audio_pos)
    print('  [%s] %d setores de audio byte a byte identicos'
          % ('OK' if audio_ok else 'FALHOU', len(st.audio_pos)))
    # 2. quadros nao editados intactos
    intactos_ok = True
    for fn, f in st.frames.items():
        if fn in editados: continue
        for cn, si in f['set'].items():
            if original[si*SEC:(si+1)*SEC] != novo[si*SEC:(si+1)*SEC]:
                intactos_ok = False; break
    print('  [%s] quadros sem legenda byte a byte identicos'
          % ('OK' if intactos_ok else 'FALHOU'))
    # 3. todos os quadros decodificam
    dec_ok = True
    st2 = Str.__new__(Str); st2.data = bytearray(novo)
    st2.frames = st.frames; st2.audio_pos = st.audio_pos
    for fn in st.frames:
        try:
            decode_syms(st.blob(fn), W, H)
        except Exception as e:
            print('    quadro %d: %s' % (fn, e)); dec_ok = False
    print('  [%s] %d quadros decodificam sem erro'
          % ('OK' if dec_ok else 'FALHOU', len(st.frames)))

    if not (audio_ok and intactos_ok and dec_ok):
        print('\nVERIFICACAO FALHOU — arquivo NAO gerado.')
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(saida)), exist_ok=True)
    open(saida, 'wb').write(novo)
    print('\nEntregue: %s' % saida)
    print('SHA-256:  %s' % hashlib.sha256(novo).hexdigest())
    print('%d quadros legendados, %d falas.' % (len(editados), len(falas)))

# ============================================================ CLI
if __name__ == '__main__':
    args = [a for a in sys.argv[1:]]
    so_validar = '--so-validar' in args
    preview = None
    if '--preview' in args:
        i = args.index('--preview'); preview = int(args[i+1])
        del args[i:i+2]
    args = [a for a in args if a != '--so-validar']
    saida = None
    if '-o' in args:
        i = args.index('-o'); saida = args[i+1]; del args[i:i+2]
    if len(args) != 2 or (saida is None and not so_validar):
        print('Uso: python3 montar.py VIDEO.MOV LEGENDAS.json -o SAIDA.MOV')
        print('     [--so-validar] [--preview N]')
        sys.exit(1)
    montar(args[0], args[1], saida or '/dev/null', so_validar, preview)
