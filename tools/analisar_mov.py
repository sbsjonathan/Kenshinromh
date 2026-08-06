#!/usr/bin/env python3
# analisar_mov.py — Analisador tecnico de FMV STR do PS1 (Rurouni Kenshin)
# Python puro, sem dependencias. Feito para rodar no a-Shell (iOS).
#
# Uso:
#   python3 analisar_mov.py RU03.MOV
#
# Saidas (na mesma pasta do video):
#   RU03_relatorio.txt  — relatorio tecnico completo
#   RU03_esqueleto.json — esqueleto de legendas com janelas de fala detectadas

import sys, os, json, struct, hashlib

SEC = 2048
MAGIC = b'\x60\x01\x01\x80'
FPS = 15
XA_RATE = 37800  # Hz, estereo

# ---------------------------------------------------------------- XA ADPCM
F0 = (0, 60, 115, 98, 122)
F1 = (0, 0, -52, -55, -60)

def decode_xa_sector(payload, state):
    """Decodifica 1 setor XA (payload 2048B = 16 sound groups) em amostras
    estereo. Retorna soma de quadrados (energia) e n de amostras por canal."""
    e_l = e_r = 0.0
    n = 0
    old_l, older_l, old_r, older_r = state
    for g in range(16):
        grp = payload[g * 128:(g + 1) * 128]
        if len(grp) < 128:
            break
        # 8 sound units; estereo: pares (L,R) -> unidades 0,2,4,6 = L; 1,3,5,7 = R
        for u in range(8):
            param = grp[4 + u] if u < 4 else grp[8 + (u - 4)]
            shift = param & 0x0F
            filt = (param >> 4) & 0x03
            if shift > 12:
                shift = 9
            left = (u & 1) == 0
            for s in range(28):
                byte = grp[16 + s * 4 + (u >> 1)]
                nib = (byte >> 4) if (u & 1) else (byte & 0x0F)
                if nib > 7:
                    nib -= 16
                samp = (nib << 12) >> shift
                if left:
                    samp += (old_l * F0[filt] + older_l * F1[filt] + 32) >> 6
                    samp = max(-32768, min(32767, samp))
                    older_l, old_l = old_l, samp
                    e_l += samp * samp
                else:
                    samp += (old_r * F0[filt] + older_r * F1[filt] + 32) >> 6
                    samp = max(-32768, min(32767, samp))
                    older_r, old_r = old_r, samp
                    e_r += samp * samp
            if left:
                n += 28
    return (e_l + e_r), n, (old_l, older_l, old_r, older_r)

# ---------------------------------------------------------------- parser
def analisar(caminho):
    data = open(caminho, 'rb').read()
    nsec = len(data) // SEC
    resto = len(data) % SEC

    frames = {}          # frame_no -> dict
    audio_secs = []      # (indice_setor, rms)
    outros = []
    xa_state = (0, 0, 0, 0)
    amostras_total = 0

    for s in range(nsec):
        c = data[s * SEC:(s + 1) * SEC]
        if c[:4] == MAGIC:
            chunk_no, chunks = struct.unpack('<HH', c[4:8])
            frame_no, = struct.unpack('<I', c[8:12])
            fsz, = struct.unpack('<I', c[12:16])
            w, h = struct.unpack('<HH', c[16:20])
            f = frames.setdefault(frame_no, {
                'chunks_decl': chunks, 'w': w, 'h': h,
                'setores': [], 'dados': {}, 'fsz': fsz})
            f['setores'].append(s)
            f['dados'][chunk_no] = c[32:]
        else:
            energia, n, xa_state = decode_xa_sector(c, xa_state)
            rms = (energia / max(1, 2 * n)) ** 0.5
            audio_secs.append((s, rms, amostras_total))
            amostras_total += n

    # montar quadros e extrair metadados do bitstream
    ordem = sorted(frames)
    linhas = []
    pretos = {}
    for fn in ordem:
        f = frames[fn]
        blob = b''.join(f['dados'][i] for i in sorted(f['dados']))
        completo = len(f['dados']) == f['chunks_decl']
        # header BS: u16 codes, u16 0x3800, u16 qscale, u16 versao
        qscale, versao = struct.unpack('<HH', blob[4:8])
        usado = f['fsz'] if f['fsz'] else len(blob)
        cap = f['chunks_decl'] * 2016
        h_ = hashlib.md5(blob[:usado]).hexdigest()
        pretos.setdefault(h_, []).append(fn)
        linhas.append({
            'quadro': fn, 'chunks': f['chunks_decl'], 'usado': usado,
            'cap': cap, 'livre': cap - usado, 'qscale': qscale,
            'versao': versao, 'completo': completo, 'md5': h_})

    # cortes de cena = sequencias CONSECUTIVAS de quadros identicos (>=2)
    repetidos = []
    i = 0
    while i < len(linhas):
        j = i
        while (j + 1 < len(linhas)
               and linhas[j + 1]['md5'] == linhas[i]['md5']
               and linhas[j + 1]['quadro'] == linhas[j]['quadro'] + 1):
            j += 1
        if j > i:
            repetidos.append((linhas[i]['quadro'], linhas[j]['quadro']))
        i = j + 1

    # ------------------------------------------------------------ VAD
    # energia suavizada -> janelas acima do limiar
    falas = []
    if audio_secs:
        rmss = [r for _, r, _ in audio_secs]
        suave = []
        for i in range(len(rmss)):
            j0, j1 = max(0, i - 1), min(len(rmss), i + 2)
            suave.append(sum(rmss[j0:j1]) / (j1 - j0))
        piso = sorted(suave)[max(0, len(suave) // 10)]
        pico = max(suave)
        limiar = piso + 0.22 * (pico - piso)
        dentro = False
        ini = 0
        for i, v in enumerate(suave):
            if not dentro and v >= limiar:
                dentro, ini = True, i
            elif dentro and v < limiar:
                dentro = False
                falas.append((ini, i - 1))
        if dentro:
            falas.append((ini, len(suave) - 1))
        # fundir janelas separadas por < 4 setores (~190ms) e descartar < 3
        fundidas = []
        for a, b in falas:
            if fundidas and a - fundidas[-1][1] < 4:
                fundidas[-1][1] = b
            else:
                fundidas.append([a, b])
        falas = [(a, b) for a, b in fundidas if b - a >= 3]

    def sec_audio_para_quadro(idx):
        t = audio_secs[idx][2] / XA_RATE
        return int(round(t * FPS))

    def ts(q):
        t = q / FPS
        h = int(t // 3600); m = int(t % 3600 // 60)
        s_ = t % 60
        return f"{h:02d}:{m:02d}:{s_:06.3f}".replace('.', ',')

    # ------------------------------------------------------------ relatorio
    base = os.path.splitext(os.path.basename(caminho))[0]
    dur = len(ordem) / FPS
    rel = []
    A = rel.append
    A(f"RELATORIO TECNICO — {os.path.basename(caminho)}")
    A("=" * 60)
    A(f"Tamanho: {len(data)} bytes | setores 2048B: {nsec}"
      + (f" | RESTO ANOMALO: {resto}B" if resto else ""))
    A(f"Setores video: {sum(len(f['setores']) for f in frames.values())}"
      f" | audio: {len(audio_secs)} | outros: {len(outros)}")
    if linhas:
        A(f"Resolucao: {frames[ordem[0]]['w']}x{frames[ordem[0]]['h']}"
          f" | bitstream v{linhas[0]['versao']}")
    A(f"Quadros: {len(ordem)} ({ordem[0]}..{ordem[-1]}) | {FPS} fps"
      f" | duracao {dur:.2f}s")
    incompletos = [l['quadro'] for l in linhas if not l['completo']]
    A(f"Quadros incompletos: {len(incompletos)}"
      + (f" -> {incompletos}" if incompletos else " (OK)"))
    apertados = [l for l in linhas if l['chunks'] <= 8]
    A(f"Quadros apertados (<=8 chunks): {len(apertados)}")
    A("")
    A("CORTES DE CENA (quadros identicos consecutivos):")
    if repetidos:
        for a, b in repetidos:
            A(f"  quadros {a}..{b} congelados ({b - a + 1} quadros identicos)")
    else:
        A("  nenhum")
    A("")
    A("JANELAS DE FALA DETECTADAS (energia do audio XA):")
    if falas:
        for i, (a, b) in enumerate(falas, 1):
            qa, qb = sec_audio_para_quadro(a), sec_audio_para_quadro(b)
            A(f"  #{i}: quadros {qa}-{qb} | {ts(qa)} --> {ts(qb)}")
    else:
        A("  nenhuma (silencio ou so musica uniforme)")
    A("")
    A("MAPA DE QUADROS (quadro | chunks | usado/cap | livre | qscale):")
    for l in linhas:
        alerta = "  <APERTADO>" if l['chunks'] <= 8 else ""
        A(f"  {l['quadro']:4d} | {l['chunks']:2d} | {l['usado']:5d}/{l['cap']:5d}"
          f" | {l['livre']:5d} | q{l['qscale']}{alerta}")

    rel_path = os.path.join(os.path.dirname(os.path.abspath(caminho)),
                            base + "_relatorio.txt")
    open(rel_path, 'w').write("\n".join(rel) + "\n")

    # ------------------------------------------------------------ esqueleto
    esq = {
        "video": os.path.basename(caminho),
        "fps": FPS,
        "padrao": {"fonte": 13, "contorno": 1, "posicao": "inferior_centro"},
        "falas": []
    }
    for a, b in falas:
        esq["falas"].append({
            "quadro_ini": sec_audio_para_quadro(a),
            "quadro_fim": sec_audio_para_quadro(b),
            "texto": ""})
    esq_path = os.path.join(os.path.dirname(os.path.abspath(caminho)),
                            base + "_esqueleto.json")
    open(esq_path, 'w').write(
        json.dumps(esq, ensure_ascii=False, indent=2) + "\n")

    print(f"OK: {rel_path}")
    print(f"OK: {esq_path}")
    print(f"{len(ordem)} quadros | {len(falas)} janelas de fala"
          f" | {len(apertados)} quadros apertados")

def escolher_video():
    """Modo interativo: lista os RUxx.MOV da pasta e pergunta qual analisar."""
    # procura a pasta extract a partir do diretorio atual
    base = 'extract' if os.path.isdir('extract') else '.'
    disponiveis = {}
    for nome in os.listdir(base):
        up = nome.upper()
        if up.startswith('RU') and up.endswith('.MOV'):
            # extrai o numero (RU03.MOV -> 3, RU10.MOV -> 10)
            num = ''.join(ch for ch in up[2:] if ch.isdigit())
            if num:
                disponiveis[int(num)] = os.path.join(base, nome)
    if disponiveis:
        nums = sorted(disponiveis)
        print("Videos encontrados em '%s/': %s"
              % (base, ", ".join("%02d" % n for n in nums)))
    else:
        print("Nenhum RUxx.MOV encontrado em '%s/'." % base)
    resp = input("Qual RU voce quer analisar? (ex: 03): ").strip()
    num = ''.join(ch for ch in resp if ch.isdigit())
    if not num:
        print("Entrada invalida.")
        sys.exit(1)
    n = int(num)
    if n in disponiveis:
        return disponiveis[n]
    # tenta montar o nome mesmo que o listdir nao ache (case/acentos)
    tent = os.path.join(base, "RU%02d.MOV" % n)
    if os.path.isfile(tent):
        return tent
    print("Nao achei RU%02d.MOV em '%s/'." % (n, base))
    sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) == 2:
        analisar(sys.argv[1])
    else:
        analisar(escolher_video())
