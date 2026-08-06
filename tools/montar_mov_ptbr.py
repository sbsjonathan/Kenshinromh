#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Montador de legendas para os RUxx.MOV de Rurouni Kenshin (PS1).

- Entrada: MOV original em setores de 2048 bytes + JSON de legendas.
- Saída: MOV de mesmo tamanho, mesma quantidade de setores e mesmo áudio.
- Vídeo: STR/MDEC v2 ou v3, 15 fps.
- Estratégia: preserva os códigos MDEC dos macroblocos sem alteração e
  recomprime somente os macroblocos tocados pela legenda.
- A compressão adicional conserva a amplitude dos coeficientes MDEC e nunca
  diminui durante uma mesma combinação de legendas. Isso evita a cintilação
  que ocorria quando o qscale original alternava entre quadros.
- Reserva uma pequena margem no fim de cada quadro e valida, por decodificação,
  todos os quadros modificados antes de aprovar a saída.

Este programa contém uma implementação independente dos formatos STR/MDEC,
baseada em documentação pública e no código-fonte do jPSXdec. Consulte
LICENCAS.txt no pacote.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VIDEO_MAGIC = 0x80010160
SECTOR_SIZE = 2048
VIDEO_HEADER_SIZE = 32
VIDEO_PAYLOAD_SIZE = 2016
FPS_DEFAULT = 15
DEFAULT_FRAME_MARGIN = 128
MIN_FRAME_MARGIN = 16
MAX_INTERNAL_SQUASH = 255

QMAT = [
    2,16,19,22,26,27,29,34,
    16,16,22,24,27,29,34,37,
    19,22,26,27,29,34,34,38,
    22,22,26,27,29,34,37,40,
    22,26,27,29,32,35,40,48,
    26,27,29,32,35,40,48,58,
    26,27,29,34,38,46,56,69,
    27,29,35,38,46,56,69,83,
]
RZZ = [
    0,1,8,16,9,2,3,10,
    17,24,32,25,18,11,4,5,
    12,19,26,33,40,48,41,34,
    27,20,13,6,7,14,21,28,
    35,42,49,56,57,50,43,36,
    29,22,15,23,30,37,44,51,
    58,59,52,45,38,31,39,46,
    53,60,61,54,47,55,62,63,
]

# MPEG-1 AC VLC table used by STR v2/v3. The sign bit is appended to each base.
AC_BASE = [
('11',0,1),('011',1,1),('0100',0,2),('0101',2,1),('00101',0,3),('00110',4,1),('00111',3,1),('000100',7,1),('000101',6,1),('000110',1,2),('000111',5,1),('0000100',2,2),('0000101',9,1),('0000110',0,4),('0000111',8,1),('00100000',13,1),('00100001',0,6),('00100010',12,1),('00100011',11,1),('00100100',3,2),('00100101',1,3),('00100110',0,5),('00100111',10,1),('0000001000',16,1),('0000001001',5,2),('0000001010',0,7),('0000001011',2,3),('0000001100',1,4),('0000001101',15,1),('0000001110',14,1),('0000001111',4,2),('000000010000',0,11),('000000010001',8,2),('000000010010',4,3),('000000010011',0,10),('000000010100',2,4),('000000010101',7,2),('000000010110',21,1),('000000010111',20,1),('000000011000',0,9),('000000011001',19,1),('000000011010',18,1),('000000011011',1,5),('000000011100',3,3),('000000011101',0,8),('000000011110',6,2),('000000011111',17,1),('0000000010000',10,2),('0000000010001',9,2),('0000000010010',5,3),('0000000010011',3,4),('0000000010100',2,5),('0000000010101',1,7),('0000000010110',1,6),('0000000010111',0,15),('0000000011000',0,14),('0000000011001',0,13),('0000000011010',0,12),('0000000011011',26,1),('0000000011100',25,1),('0000000011101',24,1),('0000000011110',23,1),('0000000011111',22,1),('00000000010000',0,31),('00000000010001',0,30),('00000000010010',0,29),('00000000010011',0,28),('00000000010100',0,27),('00000000010101',0,26),('00000000010110',0,25),('00000000010111',0,24),('00000000011000',0,23),('00000000011001',0,22),('00000000011010',0,21),('00000000011011',0,20),('00000000011100',0,19),('00000000011101',0,18),('00000000011110',0,17),('00000000011111',0,16),('000000000010000',0,40),('000000000010001',0,39),('000000000010010',0,38),('000000000010011',0,37),('000000000010100',0,36),('000000000010101',0,35),('000000000010110',0,34),('000000000010111',0,33),('000000000011000',0,32),('000000000011001',1,14),('000000000011010',1,13),('000000000011011',1,12),('000000000011100',1,11),('000000000011101',1,10),('000000000011110',1,9),('000000000011111',1,8),('0000000000010000',1,18),('0000000000010001',1,17),('0000000000010010',1,16),('0000000000010011',1,15),('0000000000010100',6,3),('0000000000010101',16,2),('0000000000010110',15,2),('0000000000010111',14,2),('0000000000011000',13,2),('0000000000011001',12,2),('0000000000011010',11,2),('0000000000011011',31,1),('0000000000011100',30,1),('0000000000011101',29,1),('0000000000011110',28,1),('0000000000011111',27,1)
]
AC_ENCODE = {(run, level): bits for bits, run, level in AC_BASE}

CHROMA_DC = [
    ('00',0,0),('01',1,1),('10',2,3),('110',3,7),('1110',4,15),
    ('11110',5,31),('111110',6,63),('1111110',7,127),('11111110',8,255),
]
LUMA_DC = [
    ('00',1,1),('01',2,3),('100',0,0),('101',3,7),('110',4,15),
    ('1110',5,31),('11110',6,63),('111110',7,127),('1111110',8,255),
]

# Cosine matrix used by the PS1 MDEC implementation (scaled by 65536 originally).
_COS_I = [
    23170,23170,23170,23170,23170,23170,23170,23170,
    32138,27245,18204,6392,-6393,-18205,-27246,-32139,
    30273,12539,-12540,-30274,-30274,-12540,12539,30273,
    27245,-6393,-32139,-18205,18204,32138,6392,-27246,
    23170,-23171,-23171,23170,23170,-23171,-23171,23170,
    18204,-32139,6392,27245,-27246,-6393,32138,-18205,
    12539,-30274,30273,-12540,-12540,30273,-30274,12539,
    6392,-18205,27245,-32139,32138,-27246,18204,-6393,
]
COS = [v / 65536.0 for v in _COS_I]

W1,W2,W3,W4,W5,W6,W7 = 22725,21407,19266,16383,12873,8867,4520

# Rasterized alphabet. Generated from a bold sans font; no font file is required.
_FONT_DATA_JSON = r'''{"ref":32,"width":832,"height":520,"cell_w":52,"cell_h":52,"metrics":{" ":{"x":0,"y":0,"w":52,"h":52,"advance":11.140625,"baseline":38},"!":{"x":52,"y":0,"w":52,"h":52,"advance":14.59375,"baseline":38},"\"":{"x":104,"y":0,"w":52,"h":52,"advance":16.671875,"baseline":38},"#":{"x":156,"y":0,"w":52,"h":52,"advance":26.8125,"baseline":38},"$":{"x":208,"y":0,"w":52,"h":52,"advance":22.265625,"baseline":38},"%":{"x":260,"y":0,"w":52,"h":52,"advance":32.0625,"baseline":38},"&":{"x":312,"y":0,"w":52,"h":52,"advance":27.90625,"baseline":38},"'":{"x":364,"y":0,"w":52,"h":52,"advance":9.796875,"baseline":38},"(":{"x":416,"y":0,"w":52,"h":52,"advance":14.625,"baseline":38},")":{"x":468,"y":0,"w":52,"h":52,"advance":14.625,"baseline":38},"*":{"x":520,"y":0,"w":52,"h":52,"advance":16.734375,"baseline":38},"+":{"x":572,"y":0,"w":52,"h":52,"advance":26.8125,"baseline":38},",":{"x":624,"y":0,"w":52,"h":52,"advance":12.15625,"baseline":38},"-":{"x":676,"y":0,"w":52,"h":52,"advance":13.28125,"baseline":38},".":{"x":728,"y":0,"w":52,"h":52,"advance":12.15625,"baseline":38},"/":{"x":780,"y":0,"w":52,"h":52,"advance":11.6875,"baseline":38},"0":{"x":0,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"1":{"x":52,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"2":{"x":104,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"3":{"x":156,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"4":{"x":208,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"5":{"x":260,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"6":{"x":312,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"7":{"x":364,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"8":{"x":416,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},"9":{"x":468,"y":52,"w":52,"h":52,"advance":22.265625,"baseline":38},":":{"x":520,"y":52,"w":52,"h":52,"advance":12.796875,"baseline":38},";":{"x":572,"y":52,"w":52,"h":52,"advance":12.796875,"baseline":38},"<":{"x":624,"y":52,"w":52,"h":52,"advance":26.8125,"baseline":38},"=":{"x":676,"y":52,"w":52,"h":52,"advance":26.8125,"baseline":38},">":{"x":728,"y":52,"w":52,"h":52,"advance":26.8125,"baseline":38},"?":{"x":780,"y":52,"w":52,"h":52,"advance":18.5625,"baseline":38},"@":{"x":0,"y":104,"w":52,"h":52,"advance":32.0,"baseline":38},"A":{"x":52,"y":104,"w":52,"h":52,"advance":24.765625,"baseline":38},"B":{"x":104,"y":104,"w":52,"h":52,"advance":24.390625,"baseline":38},"C":{"x":156,"y":104,"w":52,"h":52,"advance":23.484375,"baseline":38},"D":{"x":208,"y":104,"w":52,"h":52,"advance":26.5625,"baseline":38},"E":{"x":260,"y":104,"w":52,"h":52,"advance":21.859375,"baseline":38},"F":{"x":312,"y":104,"w":52,"h":52,"advance":21.859375,"baseline":38},"G":{"x":364,"y":104,"w":52,"h":52,"advance":26.265625,"baseline":38},"H":{"x":416,"y":104,"w":52,"h":52,"advance":26.78125,"baseline":38},"I":{"x":468,"y":104,"w":52,"h":52,"advance":11.90625,"baseline":38},"J":{"x":520,"y":104,"w":52,"h":52,"advance":11.90625,"baseline":38},"K":{"x":572,"y":104,"w":52,"h":52,"advance":24.796875,"baseline":38},"L":{"x":624,"y":104,"w":52,"h":52,"advance":20.390625,"baseline":38},"M":{"x":676,"y":104,"w":52,"h":52,"advance":31.84375,"baseline":38},"N":{"x":728,"y":104,"w":52,"h":52,"advance":26.78125,"baseline":38},"O":{"x":780,"y":104,"w":52,"h":52,"advance":27.203125,"baseline":38},"P":{"x":0,"y":156,"w":52,"h":52,"advance":23.453125,"baseline":38},"Q":{"x":52,"y":156,"w":52,"h":52,"advance":27.203125,"baseline":38},"R":{"x":104,"y":156,"w":52,"h":52,"advance":24.640625,"baseline":38},"S":{"x":156,"y":156,"w":52,"h":52,"advance":23.046875,"baseline":38},"T":{"x":208,"y":156,"w":52,"h":52,"advance":21.828125,"baseline":38},"U":{"x":260,"y":156,"w":52,"h":52,"advance":25.984375,"baseline":38},"V":{"x":312,"y":156,"w":52,"h":52,"advance":24.765625,"baseline":38},"W":{"x":364,"y":156,"w":52,"h":52,"advance":35.296875,"baseline":38},"X":{"x":416,"y":156,"w":52,"h":52,"advance":24.671875,"baseline":38},"Y":{"x":468,"y":156,"w":52,"h":52,"advance":23.171875,"baseline":38},"Z":{"x":520,"y":156,"w":52,"h":52,"advance":23.203125,"baseline":38},"[":{"x":572,"y":156,"w":52,"h":52,"advance":14.625,"baseline":38},"\\":{"x":624,"y":156,"w":52,"h":52,"advance":11.6875,"baseline":38},"]":{"x":676,"y":156,"w":52,"h":52,"advance":14.625,"baseline":38},"^":{"x":728,"y":156,"w":52,"h":52,"advance":26.8125,"baseline":38},"_":{"x":780,"y":156,"w":52,"h":52,"advance":16.0,"baseline":38},"`":{"x":0,"y":208,"w":52,"h":52,"advance":16.0,"baseline":38},"a":{"x":52,"y":208,"w":52,"h":52,"advance":21.59375,"baseline":38},"b":{"x":104,"y":208,"w":52,"h":52,"advance":22.90625,"baseline":38},"c":{"x":156,"y":208,"w":52,"h":52,"advance":18.96875,"baseline":38},"d":{"x":208,"y":208,"w":52,"h":52,"advance":22.90625,"baseline":38},"e":{"x":260,"y":208,"w":52,"h":52,"advance":21.703125,"baseline":38},"f":{"x":312,"y":208,"w":52,"h":52,"advance":13.921875,"baseline":38},"g":{"x":364,"y":208,"w":52,"h":52,"advance":22.90625,"baseline":38},"h":{"x":416,"y":208,"w":52,"h":52,"advance":22.78125,"baseline":38},"i":{"x":468,"y":208,"w":52,"h":52,"advance":10.96875,"baseline":38},"j":{"x":520,"y":208,"w":52,"h":52,"advance":10.96875,"baseline":38},"k":{"x":572,"y":208,"w":52,"h":52,"advance":21.28125,"baseline":38},"l":{"x":624,"y":208,"w":52,"h":52,"advance":10.96875,"baseline":38},"m":{"x":676,"y":208,"w":52,"h":52,"advance":33.34375,"baseline":38},"n":{"x":728,"y":208,"w":52,"h":52,"advance":22.78125,"baseline":38},"o":{"x":780,"y":208,"w":52,"h":52,"advance":21.984375,"baseline":38},"p":{"x":0,"y":260,"w":52,"h":52,"advance":22.90625,"baseline":38},"q":{"x":52,"y":260,"w":52,"h":52,"advance":22.90625,"baseline":38},"r":{"x":104,"y":260,"w":52,"h":52,"advance":15.78125,"baseline":38},"s":{"x":156,"y":260,"w":52,"h":52,"advance":19.046875,"baseline":38},"t":{"x":208,"y":260,"w":52,"h":52,"advance":15.296875,"baseline":38},"u":{"x":260,"y":260,"w":52,"h":52,"advance":22.78125,"baseline":38},"v":{"x":312,"y":260,"w":52,"h":52,"advance":20.859375,"baseline":38},"w":{"x":364,"y":260,"w":52,"h":52,"advance":29.5625,"baseline":38},"x":{"x":416,"y":260,"w":52,"h":52,"advance":20.640625,"baseline":38},"y":{"x":468,"y":260,"w":52,"h":52,"advance":20.859375,"baseline":38},"z":{"x":520,"y":260,"w":52,"h":52,"advance":18.625,"baseline":38},"{":{"x":572,"y":260,"w":52,"h":52,"advance":22.78125,"baseline":38},"|":{"x":624,"y":260,"w":52,"h":52,"advance":11.6875,"baseline":38},"}":{"x":676,"y":260,"w":52,"h":52,"advance":22.78125,"baseline":38},"~":{"x":728,"y":260,"w":52,"h":52,"advance":26.8125,"baseline":38},"Á":{"x":780,"y":260,"w":52,"h":52,"advance":24.765625,"baseline":38},"À":{"x":0,"y":312,"w":52,"h":52,"advance":24.765625,"baseline":38},"Â":{"x":52,"y":312,"w":52,"h":52,"advance":24.765625,"baseline":38},"Ã":{"x":104,"y":312,"w":52,"h":52,"advance":24.765625,"baseline":38},"Ä":{"x":156,"y":312,"w":52,"h":52,"advance":24.765625,"baseline":38},"É":{"x":208,"y":312,"w":52,"h":52,"advance":21.859375,"baseline":38},"È":{"x":260,"y":312,"w":52,"h":52,"advance":21.859375,"baseline":38},"Ê":{"x":312,"y":312,"w":52,"h":52,"advance":21.859375,"baseline":38},"Ë":{"x":364,"y":312,"w":52,"h":52,"advance":21.859375,"baseline":38},"Í":{"x":416,"y":312,"w":52,"h":52,"advance":11.90625,"baseline":38},"Ì":{"x":468,"y":312,"w":52,"h":52,"advance":11.90625,"baseline":38},"Î":{"x":520,"y":312,"w":52,"h":52,"advance":11.90625,"baseline":38},"Ï":{"x":572,"y":312,"w":52,"h":52,"advance":11.90625,"baseline":38},"Ó":{"x":624,"y":312,"w":52,"h":52,"advance":27.203125,"baseline":38},"Ò":{"x":676,"y":312,"w":52,"h":52,"advance":27.203125,"baseline":38},"Ô":{"x":728,"y":312,"w":52,"h":52,"advance":27.203125,"baseline":38},"Õ":{"x":780,"y":312,"w":52,"h":52,"advance":27.203125,"baseline":38},"Ö":{"x":0,"y":364,"w":52,"h":52,"advance":27.203125,"baseline":38},"Ú":{"x":52,"y":364,"w":52,"h":52,"advance":25.984375,"baseline":38},"Ù":{"x":104,"y":364,"w":52,"h":52,"advance":25.984375,"baseline":38},"Û":{"x":156,"y":364,"w":52,"h":52,"advance":25.984375,"baseline":38},"Ü":{"x":208,"y":364,"w":52,"h":52,"advance":25.984375,"baseline":38},"Ç":{"x":260,"y":364,"w":52,"h":52,"advance":23.484375,"baseline":38},"á":{"x":312,"y":364,"w":52,"h":52,"advance":21.59375,"baseline":38},"à":{"x":364,"y":364,"w":52,"h":52,"advance":21.59375,"baseline":38},"â":{"x":416,"y":364,"w":52,"h":52,"advance":21.59375,"baseline":38},"ã":{"x":468,"y":364,"w":52,"h":52,"advance":21.59375,"baseline":38},"ä":{"x":520,"y":364,"w":52,"h":52,"advance":21.59375,"baseline":38},"é":{"x":572,"y":364,"w":52,"h":52,"advance":21.703125,"baseline":38},"è":{"x":624,"y":364,"w":52,"h":52,"advance":21.703125,"baseline":38},"ê":{"x":676,"y":364,"w":52,"h":52,"advance":21.703125,"baseline":38},"ë":{"x":728,"y":364,"w":52,"h":52,"advance":21.703125,"baseline":38},"í":{"x":780,"y":364,"w":52,"h":52,"advance":10.96875,"baseline":38},"ì":{"x":0,"y":416,"w":52,"h":52,"advance":10.96875,"baseline":38},"î":{"x":52,"y":416,"w":52,"h":52,"advance":10.96875,"baseline":38},"ï":{"x":104,"y":416,"w":52,"h":52,"advance":10.96875,"baseline":38},"ó":{"x":156,"y":416,"w":52,"h":52,"advance":21.984375,"baseline":38},"ò":{"x":208,"y":416,"w":52,"h":52,"advance":21.984375,"baseline":38},"ô":{"x":260,"y":416,"w":52,"h":52,"advance":21.984375,"baseline":38},"õ":{"x":312,"y":416,"w":52,"h":52,"advance":21.984375,"baseline":38},"ö":{"x":364,"y":416,"w":52,"h":52,"advance":21.984375,"baseline":38},"ú":{"x":416,"y":416,"w":52,"h":52,"advance":22.78125,"baseline":38},"ù":{"x":468,"y":416,"w":52,"h":52,"advance":22.78125,"baseline":38},"û":{"x":520,"y":416,"w":52,"h":52,"advance":22.78125,"baseline":38},"ü":{"x":572,"y":416,"w":52,"h":52,"advance":22.78125,"baseline":38},"ç":{"x":624,"y":416,"w":52,"h":52,"advance":18.96875,"baseline":38},"Ñ":{"x":676,"y":416,"w":52,"h":52,"advance":26.78125,"baseline":38},"ñ":{"x":728,"y":416,"w":52,"h":52,"advance":22.78125,"baseline":38},"ª":{"x":780,"y":416,"w":52,"h":52,"advance":18.046875,"baseline":38},"º":{"x":0,"y":468,"w":52,"h":52,"advance":18.046875,"baseline":38},"–":{"x":52,"y":468,"w":52,"h":52,"advance":16.0,"baseline":38},"—":{"x":104,"y":468,"w":52,"h":52,"advance":32.0,"baseline":38},"…":{"x":156,"y":468,"w":52,"h":52,"advance":32.0,"baseline":38},"“":{"x":208,"y":468,"w":52,"h":52,"advance":21.03125,"baseline":38},"”":{"x":260,"y":468,"w":52,"h":52,"advance":21.03125,"baseline":38},"‘":{"x":312,"y":468,"w":52,"h":52,"advance":12.15625,"baseline":38},"’":{"x":364,"y":468,"w":52,"h":52,"advance":12.15625,"baseline":38},"¿":{"x":416,"y":468,"w":52,"h":52,"advance":18.5625,"baseline":38},"¡":{"x":468,"y":468,"w":52,"h":52,"advance":14.59375,"baseline":38}},"data":"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"}'''


class ToolError(RuntimeError):
    pass


def jround(x: float) -> int:
    """Equivalent to Java Math.round for the values used by the encoder."""
    return math.floor(x + 0.5)


def clamp8(v: int) -> int:
    return 0 if v < 0 else 255 if v > 255 else v


def signed_bits(v: int, n: int) -> int:
    return v & ((1 << n) - 1)


class BitReader:
    def __init__(self, data: bytes, start: int = 8, end: Optional[int] = None):
        self.data = data
        self.start = start
        self.end = len(data) if end is None else end
        self.bit = 0

    def read_bit(self) -> int:
        bi = self.bit >> 3
        if self.start + bi >= self.end:
            raise ToolError('Fim inesperado do bitstream MDEC.')
        off = self.start + (bi ^ 1)
        v = (self.data[off] >> (7 - (self.bit & 7))) & 1
        self.bit += 1
        return v

    def read_unsigned(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v

    def read_signed(self, n: int) -> int:
        v = self.read_unsigned(n)
        if v & (1 << (n - 1)):
            v -= 1 << n
        return v


class BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.cur = 0
        self.nbits = 0

    def write_bit(self, b: int) -> None:
        self.cur = (self.cur << 1) | (1 if b else 0)
        self.nbits += 1
        if self.nbits == 8:
            self.out.append(self.cur)
            self.cur = 0
            self.nbits = 0

    def write_bits_str(self, s: str) -> None:
        for c in s:
            self.write_bit(c == '1')

    def write_unsigned(self, v: int, n: int) -> None:
        for shift in range(n - 1, -1, -1):
            self.write_bit((v >> shift) & 1)

    def finish_le16(self) -> bytes:
        if self.nbits:
            self.cur <<= 8 - self.nbits
            self.out.append(self.cur)
            self.cur = 0
            self.nbits = 0
        while len(self.out) % 4:
            self.out.append(0)
        mapped = bytearray(len(self.out))
        for i, b in enumerate(self.out):
            mapped[i ^ 1] = b
        return bytes(mapped)


# AC decoder trie.
def _make_ac_trie():
    root = {}
    def add(bits, val):
        n = root
        for b in bits:
            n = n.setdefault(b, {})
        n['v'] = val
    add('10', ('eob',))
    add('000001', ('escape',))
    for bits, run, level in AC_BASE:
        add(bits + '0', ('ac', run, level))
        add(bits + '1', ('ac', run, -level))
    return root

AC_TRIE = _make_ac_trie()


def read_ac(br: BitReader):
    n = AC_TRIE
    for _ in range(17):
        n = n.get('1' if br.read_bit() else '0')
        if n is None:
            raise ToolError('Código AC inválido.')
        if 'v' in n:
            return n['v']
    raise ToolError('Código AC longo demais.')


def read_v3_diff(br: BitReader, table) -> int:
    bits = ''
    for _ in range(8):
        bits += '1' if br.read_bit() else '0'
        for prefix, n, maxv in table:
            if prefix == bits:
                if n == 0:
                    return 0
                v = br.read_unsigned(n)
                if (v & (1 << (n - 1))) == 0:
                    v -= maxv
                return v * 4
    raise ToolError('DC v3 inválido.')


def write_v3_diff(bw: BitWriter, diff: int, table) -> None:
    if diff % 4:
        raise ToolError(f'DC v3 não múltiplo de 4: {diff}')
    d = diff // 4
    for prefix, n, maxv in table:
        if n == 0:
            if d == 0:
                bw.write_bits_str(prefix)
                return
            continue
        half = 1 << (n - 1)
        if half <= d <= maxv:
            bw.write_bits_str(prefix)
            bw.write_unsigned(d, n)
            return
        if -maxv <= d <= -half:
            raw = d + maxv
            bw.write_bits_str(prefix)
            bw.write_unsigned(raw, n)
            return
    raise ToolError(f'Diferença DC v3 fora da tabela: {diff}')


@dataclass
class BlockCodes:
    dc: int
    ac: List[Tuple[int, int]]

@dataclass
class FrameInfo:
    order_index: int
    frame_number: int
    chunks: int
    used_size: int
    width: int
    height: int
    qscale: int
    version: int
    sector_by_chunk: List[int]

@dataclass
class MovInfo:
    path: Path
    frames: List[FrameInfo]
    total_sectors: int
    width: int
    height: int
    version: int
    audio_hash: str
    nonvideo_sectors: int


def inspect_mov(path: Path) -> MovInfo:
    size = path.stat().st_size
    if size % SECTOR_SIZE:
        raise ToolError(
            f'{path.name} possui {size} bytes. Esta ferramenta exige o MOV extraído em setores de 2048 bytes.'
        )
    total = size // SECTOR_SIZE
    frames_map: Dict[int, dict] = {}
    ah = hashlib.sha256()
    nonvideo = 0
    with path.open('rb') as f:
        for si in range(total):
            sec = f.read(SECTOR_SIZE)
            if len(sec) != SECTOR_SIZE:
                raise ToolError('Leitura incompleta do MOV.')
            magic = struct.unpack_from('<I', sec, 0)[0]
            if magic == VIDEO_MAGIC:
                chunk, count = struct.unpack_from('<HH', sec, 4)
                num = struct.unpack_from('<I', sec, 8)[0]
                used = struct.unpack_from('<I', sec, 12)[0]
                width, height = struct.unpack_from('<HH', sec, 16)
                qscale, version = struct.unpack_from('<HH', sec, 24)
                if not count or chunk >= count:
                    raise ToolError(f'Cabeçalho de vídeo inválido no setor {si}.')
                d = frames_map.setdefault(num, {
                    'count': count, 'used': used, 'width': width, 'height': height,
                    'qscale': qscale, 'version': version,
                    'sectors': [None] * count,
                })
                if d['count'] != count or d['width'] != width or d['height'] != height:
                    raise ToolError(f'Cabeçalhos inconsistentes no quadro {num}.')
                if d['sectors'][chunk] is not None:
                    raise ToolError(f'Fragmento duplicado no quadro {num}.')
                d['sectors'][chunk] = si
            else:
                ah.update(sec)
                nonvideo += 1
    frames = []
    for idx, num in enumerate(sorted(frames_map)):
        d = frames_map[num]
        if any(v is None for v in d['sectors']):
            raise ToolError(f'Quadro {num} incompleto.')
        frames.append(FrameInfo(idx, num, d['count'], d['used'], d['width'], d['height'],
                                d['qscale'], d['version'], list(d['sectors'])))
    if not frames:
        raise ToolError('Nenhum quadro STR/MDEC encontrado.')
    w, h, v = frames[0].width, frames[0].height, frames[0].version
    if any(fr.width != w or fr.height != h or fr.version != v for fr in frames):
        raise ToolError('O MOV muda de resolução ou versão no meio do arquivo; ainda não suportado.')
    return MovInfo(path, frames, total, w, h, v, ah.hexdigest(), nonvideo)


def read_frame_data(fh, fr: FrameInfo) -> bytes:
    buf = bytearray(fr.chunks * VIDEO_PAYLOAD_SIZE)
    for chunk, si in enumerate(fr.sector_by_chunk):
        fh.seek(si * SECTOR_SIZE)
        sec = fh.read(SECTOR_SIZE)
        if len(sec) != SECTOR_SIZE:
            raise ToolError(f'Não foi possível ler o setor {si}.')
        buf[chunk * VIDEO_PAYLOAD_SIZE:(chunk + 1) * VIDEO_PAYLOAD_SIZE] = sec[VIDEO_HEADER_SIZE:SECTOR_SIZE]
    used = min(fr.used_size or len(buf), len(buf))
    return bytes(buf[:used])


def decode_frame_codes(data: bytes, width: int, height: int, version: int, qscale: int) -> List[List[BlockCodes]]:
    if len(data) < 8 or struct.unpack_from('<H', data, 2)[0] != 0x3800:
        raise ToolError('Cabeçalho interno STR inválido.')
    header_q, header_v = struct.unpack_from('<HH', data, 4)
    if header_v != version:
        raise ToolError(f'Versão do quadro mudou: setor v{version}, dados v{header_v}.')
    if header_q != qscale:
        qscale = header_q
    mbw = (width + 15) // 16
    mbh = (height + 15) // 16
    br = BitReader(data)
    out: List[List[BlockCodes]] = []
    pcr = pcb = py = 0
    for _mb in range(mbw * mbh):
        blocks = []
        for b in range(6):
            if version == 2:
                dc = br.read_signed(10)
            elif version == 3:
                if b == 0:
                    pcr += read_v3_diff(br, CHROMA_DC); dc = pcr
                elif b == 1:
                    pcb += read_v3_diff(br, CHROMA_DC); dc = pcb
                else:
                    py += read_v3_diff(br, LUMA_DC); dc = py
            else:
                raise ToolError(f'STR v{version} não suportado.')
            pos = 0
            ac = []
            while True:
                token = read_ac(br)
                if token[0] == 'eob':
                    break
                if token[0] == 'escape':
                    run = br.read_unsigned(6)
                    level = br.read_signed(10)
                else:
                    _, run, level = token
                pos += run + 1
                if pos > 63:
                    raise ToolError('RLC fora do bloco.')
                ac.append((run, level))
            blocks.append(BlockCodes(dc, ac))
        out.append(blocks)
    return out


def encode_ac(bw: BitWriter, run: int, level: int) -> None:
    bits = AC_ENCODE.get((run, abs(level)))
    if bits is not None:
        bw.write_bits_str(bits)
        bw.write_bit(1 if level < 0 else 0)
    else:
        if not (0 <= run <= 63 and -512 <= level <= 511 and level != 0):
            raise ToolError(f'Coeficiente AC fora do formato: run={run}, nível={level}')
        bw.write_bits_str('000001')
        bw.write_unsigned(run, 6)
        bw.write_unsigned(signed_bits(level, 10), 10)


def half_ceiling_32(code_count: int) -> int:
    return ((((code_count + 1) // 2) + 31) & ~31) & 0xffff


def encode_frame_codes(mbs: Sequence[Sequence[BlockCodes]], version: int, qscale: int) -> bytes:
    bw = BitWriter()
    code_count = 0
    pcr = pcb = py = 0
    for blocks in mbs:
        if len(blocks) != 6:
            raise ToolError('Macrobloco inválido.')
        for b, block in enumerate(blocks):
            dc = block.dc
            if version == 2:
                if not -512 <= dc <= 511:
                    raise ToolError(f'DC v2 fora da faixa: {dc}')
                bw.write_unsigned(signed_bits(dc, 10), 10)
            elif version == 3:
                dc = jround(dc / 4.0) * 4
                if b == 0:
                    write_v3_diff(bw, dc - pcr, CHROMA_DC); pcr = dc
                elif b == 1:
                    write_v3_diff(bw, dc - pcb, CHROMA_DC); pcb = dc
                else:
                    write_v3_diff(bw, dc - py, LUMA_DC); py = dc
            else:
                raise ToolError(f'STR v{version} não suportado.')
            code_count += 1
            for run, level in block.ac:
                encode_ac(bw, run, level)
                code_count += 1
            bw.write_bits_str('10')
            code_count += 1
    bw.write_bits_str('0111111111')
    bitstream = bw.finish_le16()
    header = struct.pack('<HHHH', half_ceiling_32(code_count), 0x3800, qscale, version)
    return header + bitstream


def block_to_coeff(block: BlockCodes, qscale: int) -> List[int]:
    c = [0] * 64
    c[0] = block.dc * QMAT[0]
    pos = 0
    for run, level in block.ac:
        pos += run + 1
        if pos > 63:
            raise ToolError('RLC fora do bloco.')
        z = RZZ[pos]
        c[z] = (level * QMAT[z] * qscale + 4) >> 3
    return c


def _idct_1d(c: List[int], o: int, s: int, shift: int, out_off: int, out: List[int]) -> None:
    i0,i1,i2,i3,i4,i5,i6,i7 = (c[o + k*s] for k in range(8))
    last = (i4 | i5 | i6 | i7) == 0
    dc = i0 * W4 + (1 << (shift - 1))
    m11,m13,m15,m17 = i1*W1,i1*W3,i1*W5,i1*W7
    m22,m26 = i2*W2,i2*W6
    m31,m33,m35,m37 = i3*W1,i3*W3,i3*W5,i3*W7
    e0,e1,e2,e3 = dc+m22,dc+m26,dc-m26,dc-m22
    o0,o1,o2,o3 = m11+m33,m13-m37,m15-m31,m17-m35
    if not last:
        m44=i4*W4
        m51,m53,m55,m57=i5*W1,i5*W3,i5*W5,i5*W7
        m62,m66=i6*W2,i6*W6
        m71,m73,m75,m77=i7*W1,i7*W3,i7*W5,i7*W7
        e0 += m44+m66; e1 += -m44-m62; e2 += -m44+m62; e3 += m44-m66
        o0 += m55+m77; o1 += -m51-m75; o2 += m57+m73; o3 += m53-m71
    out[out_off+o] = (e0+o0) >> shift
    out[out_off+o+7*s] = (e0-o0) >> shift
    out[out_off+o+s] = (e1+o1) >> shift
    out[out_off+o+6*s] = (e1-o1) >> shift
    out[out_off+o+2*s] = (e2+o2) >> shift
    out[out_off+o+5*s] = (e2-o2) >> shift
    out[out_off+o+3*s] = (e3+o3) >> shift
    out[out_off+o+4*s] = (e3-o3) >> shift


def idct(block: List[int]) -> List[int]:
    a = list(block)
    for i in range(8):
        _idct_1d(a, i*8, 1, 11, 0, a)
    out = [0] * 64
    for i in range(8):
        _idct_1d(a, i, 8, 20, 0, out)
    return out


def dct(block: Sequence[float]) -> List[float]:
    temp = [0.0] * 64
    out = [0.0] * 64
    for x in range(8):
        for y in range(8):
            s = 0.0
            for i in range(8):
                s += block[x + i*8] * COS[i + y*8]
            temp[x + y*8] = s
    for x in range(8):
        for y in range(8):
            s = 0.0
            for i in range(8):
                s += COS[i + x*8] * temp[i + y*8]
            out[x + y*8] = s
    return out


def macroblock_to_rgb(blocks: Sequence[BlockCodes], qscale: int) -> bytearray:
    vals = [idct(block_to_coeff(b, qscale)) for b in blocks]
    cr, cb, y1, y2, y3, y4 = vals
    rgb = bytearray(16 * 16 * 3)
    for y in range(16):
        for x in range(16):
            if y < 8:
                yy = y1[y*8+x] if x < 8 else y2[y*8+(x-8)]
            else:
                yy = y3[(y-8)*8+x] if x < 8 else y4[(y-8)*8+(x-8)]
            cc = cb[(y//2)*8 + x//2]
            rr = cr[(y//2)*8 + x//2]
            ys = yy + 128
            # PSX YCbCr -> RGB, integer approximation used by the player.
            def shr(v, n):
                return 0 if v == 0 else (v >> n) + ((v >> (n-1)) & 1)
            r = clamp8(ys + shr(91893 * rr, 16))
            g = clamp8(ys + shr(-22525 * cc - 46812 * rr, 16))
            b = clamp8(ys + shr(116224 * cc, 16))
            o = (y*16+x)*3
            rgb[o:o+3] = bytes((r,g,b))
    return rgb


def macroblock_rgb_to_dct(rgb: Sequence[int]) -> List[List[float]]:
    yv = [0.0] * 256
    cb = [0.0] * 64
    cr = [0.0] * 64
    for yy in range(0,16,2):
        for xx in range(0,16,2):
            cbs = crs = 0.0
            for dy in range(2):
                for dx in range(2):
                    p = ((yy+dy)*16 + (xx+dx))*3
                    r,g,b = rgb[p],rgb[p+1],rgb[p+2]
                    rm,gm,bm = r-128,g-128,b-128
                    yval = rm*0.299 + gm*0.587 + bm*0.114
                    yv[(yy+dy)*16 + xx+dx] = yval
                    cbs += rm*(-0.16871) + gm*(-0.33130) + bm*0.5
                    crs += rm*0.5 + gm*(-0.4187) + bm*(-0.0813)
            cb[(yy//2)*8 + xx//2] = cbs / 4.0
            cr[(yy//2)*8 + xx//2] = crs / 4.0
    blocks = [cr, cb]
    for by,bx in ((0,0),(0,8),(8,0),(8,8)):
        block = [0.0] * 64
        for y in range(8):
            block[y*8:(y+1)*8] = yv[(by+y)*16+bx:(by+y)*16+bx+8]
        blocks.append(block)
    return [dct(b) for b in blocks]


def quantize_dct(blocks_dct: Sequence[Sequence[float]], frame_q: int, squash_q: int) -> List[BlockCodes]:
    """Converte DCT em coeficientes expressos no qscale original do quadro.

    ``squash_q`` é apenas o passo usado para eliminar detalhes pequenos. O
    decodificador do jogo continuará usando ``frame_q``; por isso, depois da
    quantização grosseira, o nível deve ser convertido de volta para unidades
    de ``frame_q``. A versão anterior aplicava a razão inversa e reduzia a
    energia aproximadamente pelo quadrado da compressão, fazendo as letras
    alternarem entre brancas, cinzas e quase invisíveis.
    """
    result = []
    for d in blocks_dct:
        dc = jround(d[0] / QMAT[0])
        ac = []
        zeros = 0
        for pos in range(1,64):
            z = RZZ[pos]
            dblq = d[z] * 8.0 / QMAT[z]
            if squash_q == frame_q:
                level = jround(dblq / frame_q)
            else:
                coarse = jround(dblq / squash_q)
                level = jround(coarse * squash_q / float(frame_q))
            if level == 0:
                zeros += 1
            else:
                if not -512 <= level <= 511:
                    raise ToolError('Energia excessiva no macrobloco.')
                ac.append((zeros, level))
                zeros = 0
        result.append(BlockCodes(dc, ac))
    return result


class RasterFont:
    def __init__(self):
        d = json.loads(_FONT_DATA_JSON)
        self.ref = d['ref']
        self.w = d['width']; self.h = d['height']
        self.metrics = d['metrics']
        self.data = zlib.decompress(base64.b64decode(d['data']))

    def advance(self, ch: str, size: int) -> int:
        m = self.metrics.get(ch) or self.metrics.get('?')
        return max(1, jround(m['advance'] * size / self.ref))

    def measure(self, text: str, size: int) -> int:
        return sum(self.advance(c, size) for c in text)

    def draw_glyph(self, alpha: bytearray, fw: int, fh: int, x: int, baseline: int,
                   ch: str, size: int, value: int = 255) -> int:
        m = self.metrics.get(ch) or self.metrics.get('?')
        scale = size / self.ref
        sw = max(1, jround(m['w'] * scale)); sh = max(1, jround(m['h'] * scale))
        sx0, sy0 = m['x'], m['y']
        dest_y0 = baseline - jround(m['baseline'] * scale)
        for dy in range(sh):
            yy = dest_y0 + dy
            if yy < 0 or yy >= fh: continue
            sy = min(m['h']-1, int(dy / scale))
            srcrow = (sy0 + sy) * self.w + sx0
            dstrow = yy * fw
            for dx in range(sw):
                xx = x + dx
                if xx < 0 or xx >= fw: continue
                sx = min(m['w']-1, int(dx / scale))
                a = self.data[srcrow + sx]
                if a:
                    # Reforço de contraste para permanecer legível após a compressão MDEC.
                    a = 255 if a >= 80 else min(255, a * 3)
                    nv = (a * value + 127) // 255
                    di = dstrow + xx
                    if nv > alpha[di]: alpha[di] = nv
        return self.advance(ch, size)

FONT = RasterFont()

@dataclass
class Overlay:
    width: int
    height: int
    rgb: bytearray
    alpha: bytearray
    touched_mbs: List[int]
    fully_opaque_mbs: set


def make_blank_overlay(w: int, h: int) -> Overlay:
    return Overlay(w,h,bytearray(w*h*3),bytearray(w*h),[],set())


def composite_color(ov: Overlay, mask: Sequence[int], color: Tuple[int,int,int]) -> None:
    for i, a in enumerate(mask):
        if not a: continue
        olda = ov.alpha[i]
        # source-over; masks used here are normally opaque or antialiased.
        na = a + (olda * (255-a) + 127)//255
        p=i*3
        if na:
            for k,c in enumerate(color):
                oldc=ov.rgb[p+k]
                num = c*a + (oldc*olda*(255-a)+127)//255
                ov.rgb[p+k] = clamp8((num + na//2)//na)
        ov.alpha[i]=na


def fill_rect(ov: Overlay, x0:int,y0:int,x1:int,y1:int,color:Tuple[int,int,int],alpha:int=255):
    x0=max(0,x0);y0=max(0,y0);x1=min(ov.width,x1);y1=min(ov.height,y1)
    for y in range(y0,y1):
        row=y*ov.width
        for x in range(x0,x1):
            i=row+x; p=i*3
            ov.rgb[p:p+3]=bytes(color); ov.alpha[i]=alpha


def draw_text_mask(w:int,h:int, lines:List[str], size:int, x:int, y:int,
                   align:str='center', line_gap:int=1, outline:int=2) -> Tuple[bytearray,bytearray]:
    white=bytearray(w*h); black=bytearray(w*h)
    line_h=max(size+2, jround(size*1.18))
    for li,line in enumerate(lines):
        lw=FONT.measure(line,size)
        if align=='left': xx=x
        elif align=='right': xx=x-lw
        else: xx=x-lw//2
        baseline=y+li*(line_h+line_gap)+size
        # black outline by drawing the same glyphs around the center.
        if outline>0:
            for oy in range(-outline,outline+1):
                for ox in range(-outline,outline+1):
                    if ox*ox+oy*oy > outline*outline+1: continue
                    gx=xx
                    for ch in line:
                        gx += FONT.draw_glyph(black,w,h,gx+ox,baseline+oy,ch,size,255)
        gx=xx
        for ch in line:
            gx += FONT.draw_glyph(white,w,h,gx,baseline,ch,size,255)
    return black,white


def position_xy(pos: str, w:int,h:int, boxw:int,boxh:int, margin:int=10) -> Tuple[int,int,str]:
    pos=(pos or 'centro').lower()
    if 'esquerda' in pos: x=margin; align='left'
    elif 'direita' in pos: x=w-margin; align='right'
    else: x=w//2; align='center'
    if 'superior' in pos: y=margin
    elif 'inferior' in pos: y=h-margin-boxh
    else: y=(h-boxh)//2
    return x,y,align


def cue_overlay(cue: dict, w:int,h:int) -> Overlay:
    ov=make_blank_overlay(w,h)
    style=(cue.get('estilo') or cue.get('tipo') or 'barra_inferior').lower()
    text=str(cue.get('portugues',cue.get('texto',''))).replace('\\n','\n')
    lines=text.splitlines() or ['']
    if style in ('barra_inferior','dialogo','fala'):
        bar_h=int(cue.get('barra_altura',48))
        bar_h=max(16,((bar_h+15)//16)*16)
        y0=h-bar_h
        safe_background=bool(cue.get('fundo_seguro',True))
        bar_alpha=255 if safe_background else int(cue.get('barra_alpha',255))
        fill_rect(ov,0,y0,w,h,(0,0,0),bar_alpha)
        size=int(cue.get('fonte',21 if h>=224 else 19))
        line_h=max(size+2,jround(size*1.18)); total=len(lines)*line_h+(len(lines)-1)
        ty=y0+(bar_h-total)//2-1
        black,white=draw_text_mask(w,h,lines,size,w//2,ty,'center',1,int(cue.get('contorno',2)))
        composite_color(ov,black,(0,0,0)); composite_color(ov,white,(255,255,255))
    else:
        size=int(cue.get('fonte',20 if style in ('sem_barra_central','credito','titulo') and h>=224 else 18 if style in ('sem_barra_central','credito','titulo') else 17 if h>=224 else 15))
        line_h=max(size+2,jround(size*1.18)); boxh=len(lines)*line_h+(len(lines)-1)
        boxw=max(FONT.measure(line,size) for line in lines)
        if 'x' in cue or 'y' in cue:
            x=int(cue.get('x',w//2)); y=int(cue.get('y',(h-boxh)//2)); align=cue.get('alinhamento','center')
        else:
            default_pos='centro' if style in ('sem_barra_central','credito','titulo') else cue.get('posicao','inferior_direita')
            x,y,align=position_xy(default_pos,w,h,boxw,boxh,int(cue.get('margem',10)))
        outline=int(cue.get('contorno',3 if 'nome' in style else 2))
        # Fundo seguro: cobre macroblocos inteiros. Além de impedir que cores
        # do filme vazem para as letras quando o quadro está cheio, uma região
        # preta e opaca requer muito menos coeficientes MDEC que a mistura do
        # texto com um fundo em movimento. Pode ser desligado por entrada com
        # "fundo_seguro": false, assumindo conscientemente o risco de cintilar.
        if bool(cue.get('fundo_seguro',True)):
            if align=='left': left=x
            elif align=='right': left=x-boxw
            else: left=x-boxw//2
            pad=max(2,outline+1)
            x0=max(0,left-pad); y0=max(0,y-pad)
            x1=min(w,left+boxw+pad); y1=min(h,y+boxh+pad)
            x0=(x0//16)*16; y0=(y0//16)*16
            x1=min(w,((x1+15)//16)*16); y1=min(h,((y1+15)//16)*16)
            fill_rect(ov,x0,y0,x1,y1,(0,0,0),255)
        black,white=draw_text_mask(w,h,lines,size,x,y,align,1,outline)
        composite_color(ov,black,(0,0,0)); composite_color(ov,white,(255,255,255))
    finalize_overlay(ov)
    return ov


def merge_overlays(overlays: Sequence[Overlay]) -> Overlay:
    if not overlays: raise ToolError('Nenhuma sobreposição ativa.')
    w,h=overlays[0].width,overlays[0].height
    out=make_blank_overlay(w,h)
    for ov in overlays:
        for i,a in enumerate(ov.alpha):
            if not a: continue
            oa=out.alpha[i]; na=a+(oa*(255-a)+127)//255; p=i*3
            for k in range(3):
                num=ov.rgb[p+k]*a+(out.rgb[p+k]*oa*(255-a)+127)//255
                out.rgb[p+k]=clamp8((num+na//2)//na) if na else 0
            out.alpha[i]=na
    finalize_overlay(out)
    return out


def finalize_overlay(ov: Overlay) -> None:
    mbw=(ov.width+15)//16; mbh=(ov.height+15)//16
    touched=[]; opaque=set()
    for mx in range(mbw):
        for my in range(mbh):
            mb=mx*mbh+my
            any_a=False; all_a=True
            for yy in range(my*16,min(my*16+16,ov.height)):
                row=yy*ov.width
                for xx in range(mx*16,min(mx*16+16,ov.width)):
                    a=ov.alpha[row+xx]
                    any_a |= a>0; all_a &= a==255
            if any_a: touched.append(mb)
            if any_a and all_a: opaque.add(mb)
    ov.touched_mbs=touched; ov.fully_opaque_mbs=opaque


def overlay_macroblock(original: Optional[Sequence[int]], ov: Overlay, mb:int) -> bytearray:
    mbw=(ov.width+15)//16; mbh=(ov.height+15)//16
    mx=mb//mbh; my=mb%mbh
    if original is None:
        rgb=bytearray(16*16*3)
    else:
        rgb=bytearray(original)
    for ly in range(16):
        y=my*16+ly
        if y>=ov.height: continue
        for lx in range(16):
            x=mx*16+lx
            if x>=ov.width: continue
            oi=y*ov.width+x; a=ov.alpha[oi]
            if not a: continue
            op=oi*3; dp=(ly*16+lx)*3
            for k in range(3):
                rgb[dp+k]=(ov.rgb[op+k]*a + rgb[dp+k]*(255-a)+127)//255
    return rgb


def patch_frame_sectors(out_fh, fr:FrameInfo, new_data:bytes) -> None:
    capacity=fr.chunks*VIDEO_PAYLOAD_SIZE
    if len(new_data)>capacity:
        raise ToolError(f'Quadro {fr.order_index} não cabe: {len(new_data)} > {capacity}.')
    padded=new_data+bytes(capacity-len(new_data))
    half=struct.unpack_from('<H',new_data,0)[0]
    q=struct.unpack_from('<H',new_data,4)[0]
    ver=struct.unpack_from('<H',new_data,6)[0]
    for chunk,si in enumerate(fr.sector_by_chunk):
        out_fh.seek(si*SECTOR_SIZE)
        sec=bytearray(out_fh.read(SECTOR_SIZE))
        if len(sec)!=SECTOR_SIZE: raise ToolError(f'Falha lendo saída no setor {si}.')
        struct.pack_into('<I',sec,12,len(new_data))
        struct.pack_into('<H',sec,20,half)
        struct.pack_into('<H',sec,22,0x3800)
        struct.pack_into('<H',sec,24,q)
        struct.pack_into('<H',sec,26,ver)
        sec[VIDEO_HEADER_SIZE:]=padded[chunk*VIDEO_PAYLOAD_SIZE:(chunk+1)*VIDEO_PAYLOAD_SIZE]
        out_fh.seek(si*SECTOR_SIZE); out_fh.write(sec)


def load_project_json(path:Path, mov:MovInfo) -> Tuple[dict,List[dict]]:
    try: d=json.loads(path.read_text(encoding='utf-8'))
    except Exception as e: raise ToolError(f'Não foi possível ler {path.name}: {e}')
    cues=d.get('entradas',d.get('legendas'))
    if not isinstance(cues,list): raise ToolError('O JSON precisa ter uma lista "entradas".')
    norm=[]
    for i,c in enumerate(cues,1):
        c=dict(c)
        if 'inicio_frame' not in c or 'fim_frame' not in c:
            raise ToolError(f'Entrada {i} sem inicio_frame/fim_frame.')
        a=int(c['inicio_frame']); b=int(c['fim_frame'])
        if a<0 or b<a or b>=len(mov.frames):
            raise ToolError(f'Entrada {i}: intervalo {a}..{b} fora de 0..{len(mov.frames)-1}.')
        if not c.get('portugues',c.get('texto')):
            raise ToolError(f'Entrada {i} sem texto em português.')
        c['_id']=c.get('id',i); c['_start']=a; c['_end']=b
        norm.append(c)
    return d,norm


def active_map(cues:List[dict], nframes:int) -> Dict[int,Tuple[int,...]]:
    events: Dict[int,List[Tuple[int,int]]] = {}
    for ci,c in enumerate(cues):
        events.setdefault(c['_start'],[]).append((1,ci))
        events.setdefault(c['_end']+1,[]).append((-1,ci))
    active=set(); result={}
    for fi in range(nframes):
        for op,ci in events.get(fi,[]):
            if op>0: active.add(ci)
            else: active.discard(ci)
        if active: result[fi]=tuple(sorted(active))
    return result


def validate_output(src:MovInfo, out_path:Path,
                    modified_frames:Optional[Iterable[int]]=None) -> dict:
    out=inspect_mov(out_path)
    problems=[]
    if out_path.stat().st_size!=src.path.stat().st_size: problems.append('tamanho mudou')
    if len(out.frames)!=len(src.frames): problems.append('quantidade de quadros mudou')
    if out.total_sectors!=src.total_sectors: problems.append('quantidade de setores mudou')
    if out.audio_hash!=src.audio_hash: problems.append('setores não-vídeo/áudio mudaram')
    # O hash acima detecta qualquer mudança no conjunto dos setores, mas esta
    # comparação também garante que eles continuam nas mesmas posições.
    audio_checked=0
    with src.path.open('rb') as original_fh, out_path.open('rb') as output_fh:
        for si in range(min(src.total_sectors,out.total_sectors)):
            before=original_fh.read(SECTOR_SIZE); after=output_fh.read(SECTOR_SIZE)
            if len(before)!=SECTOR_SIZE or len(after)!=SECTOR_SIZE:
                problems.append(f'leitura incompleta durante validação no setor {si}')
                break
            before_video=struct.unpack_from('<I',before,0)[0]==VIDEO_MAGIC
            after_video=struct.unpack_from('<I',after,0)[0]==VIDEO_MAGIC
            if before_video!=after_video:
                problems.append(f'tipo do setor {si} mudou')
                break
            if not before_video:
                audio_checked+=1
                if before!=after:
                    problems.append(f'setor de áudio/não-vídeo {si} mudou')
                    break

    decoded=0
    decode_error=None
    if modified_frames is not None and len(out.frames)==len(src.frames):
        with out_path.open('rb') as fh:
            for fi in sorted(set(int(v) for v in modified_frames)):
                if not 0<=fi<len(out.frames):
                    decode_error=f'índice de quadro inválido na validação: {fi}'
                    break
                fr=out.frames[fi]
                try:
                    decode_frame_codes(read_frame_data(fh,fr),fr.width,fr.height,
                                       fr.version,fr.qscale)
                except (ToolError,ValueError,IndexError,struct.error) as exc:
                    decode_error=f'quadro {fi} não decodifica: {exc}'
                    break
                decoded+=1
    if decode_error: problems.append(decode_error)
    return {
        'ok':not problems,'problemas':problems,'arquivo':out_path.name,
        'tamanho':out_path.stat().st_size,'quadros':len(out.frames),
        'resolucao':[out.width,out.height],'str_version':out.version,
        'setores_nao_video':out.nonvideo_sectors,'hash_audio_nao_video':out.audio_hash,
        'setores_audio_comparados_byte_a_byte':audio_checked,
        'quadros_mdec_decodificados':decoded,
    }


def build_mov(src_path:Path,json_path:Path,out_path:Path,force:bool=False) -> dict:
    src=inspect_mov(src_path)
    project,cues=load_project_json(json_path,src)
    amap=active_map(cues,len(src.frames))
    if not amap: raise ToolError('O JSON não contém nenhum quadro para modificar.')
    cue_ovs=[cue_overlay(c,src.width,src.height) for c in cues]
    combo_cache:Dict[Tuple[int,...],Overlay]={}
    synthetic_dct_cache:Dict[Tuple[Tuple[int,...],int],List[List[float]]]={}
    # Uma combinação de legendas nunca volta a usar compressão menor. Isso
    # remove a oscilação visual entre quadros consecutivos sem exigir guardar
    # o vídeo inteiro na memória (importante para filmes longos no a-Shell).
    squash_by_combo:Dict[Tuple[int,...],int]={}
    frame_margin=int(project.get('margem_bytes_quadro',DEFAULT_FRAME_MARGIN))
    if not 0<=frame_margin<=1024:
        raise ToolError('margem_bytes_quadro precisa estar entre 0 e 1024.')

    if out_path.exists() and not force:
        raise ToolError(f'{out_path} já existe. Use --force para substituir.')
    out_path.parent.mkdir(parents=True,exist_ok=True)
    tmp=out_path.with_suffix(out_path.suffix+'.tmp')
    if tmp.exists(): tmp.unlink()
    print(f'Copiando {src_path.name} para {out_path.name}...')
    shutil.copyfile(src_path,tmp)

    modified=0; max_squash=0; t0=time.time()
    min_real_margin=None; relaxed_margin_frames=0
    with src_path.open('rb') as inp, tmp.open('r+b') as out:
        total=len(amap)
        for done,(fi,combo) in enumerate(sorted(amap.items()),1):
            fr=src.frames[fi]
            ov=combo_cache.get(combo)
            if ov is None:
                ov=merge_overlays([cue_ovs[i] for i in combo])
                combo_cache[combo]=ov
            data=read_frame_data(inp,fr)
            original=decode_frame_codes(data,fr.width,fr.height,fr.version,fr.qscale)
            dct_by_mb={}
            for mb in ov.touched_mbs:
                key=(combo,mb)
                if mb in ov.fully_opaque_mbs and key in synthetic_dct_cache:
                    dct_by_mb[mb]=synthetic_dct_cache[key]
                    continue
                base=None if mb in ov.fully_opaque_mbs else macroblock_to_rgb(original[mb],fr.qscale)
                rgb=overlay_macroblock(base,ov,mb)
                mdct=macroblock_rgb_to_dct(rgb)
                dct_by_mb[mb]=mdct
                if mb in ov.fully_opaque_mbs: synthetic_dct_cache[key]=mdct

            capacity=fr.chunks*VIDEO_PAYLOAD_SIZE
            if capacity-frame_margin<=8:
                tmp.unlink(missing_ok=True)
                raise ToolError(f'Quadro {fi} não possui capacidade útil suficiente.')
            new_data=None; used_squash=None; used_margin=None
            start_q=max(1,fr.qscale,squash_by_combo.get(combo,1))
            encoded_cache:Dict[int,Optional[bytes]]={}

            def encoded_at(sq:int) -> Optional[bytes]:
                if sq in encoded_cache: return encoded_cache[sq]
                trial=list(original)
                for mb,mdct in dct_by_mb.items():
                    try: trial[mb]=quantize_dct(mdct,fr.qscale,sq)
                    except ToolError:
                        encoded_cache[sq]=None; return None
                try: enc=encode_frame_codes(trial,fr.version,fr.qscale)
                except ToolError:
                    encoded_cache[sq]=None; return None
                encoded_cache[sq]=enc; return enc

            # Ordem deliberada: primeiro mantém qualidade e a margem pedida;
            # depois conserva q<=63 relaxando somente a reserva; apenas por
            # último usa quantização interna ampliada. O valor interno não vai
            # para o cabeçalho STR, cujo qscale original permanece intacto.
            minimum_margin=min(frame_margin,MIN_FRAME_MARGIN)
            strategies=[
                (frame_margin,63),
                (minimum_margin,63),
                (frame_margin,MAX_INTERNAL_SQUASH),
                (minimum_margin,MAX_INTERNAL_SQUASH),
                (0,MAX_INTERNAL_SQUASH),
            ]
            seen=set()
            for margin,max_q in strategies:
                key=(margin,max_q)
                if key in seen: continue
                seen.add(key)
                if start_q>max_q: continue
                limit=capacity-margin
                for sq in range(start_q,max_q+1):
                    enc=encoded_at(sq)
                    if enc is not None and len(enc)<=limit:
                        new_data=enc; used_squash=sq; used_margin=margin
                        break
                if new_data is not None: break
            if new_data is None:
                tmp.unlink(missing_ok=True)
                raise ToolError(
                    f'Quadro {fi} não coube nem usando toda a capacidade e '
                    f'quantização interna {MAX_INTERNAL_SQUASH}. '
                    'Reduza o texto ou a fonte da entrada ativa.'
                )
            patch_frame_sectors(out,fr,new_data)
            squash_by_combo[combo]=used_squash or start_q
            real_margin=capacity-len(new_data)
            min_real_margin=real_margin if min_real_margin is None else min(min_real_margin,real_margin)
            if (used_margin or 0)<frame_margin: relaxed_margin_frames+=1
            modified+=1; max_squash=max(max_squash,used_squash or 0)
            if done==1 or done==total or done%10==0:
                pct=done*100/total; elapsed=time.time()-t0
                print(f'  {done:4d}/{total} quadros ({pct:5.1f}%)  qmáx={max_squash}  {elapsed:6.1f}s',flush=True)

    os.replace(tmp,out_path)
    report=validate_output(src,out_path,amap.keys())
    report.update({
        'fonte':src_path.name,'json':json_path.name,'quadros_modificados':modified,
        'qscale_squash_maximo':max_squash,'duracao_segundos':len(src.frames)/float(project.get('fps',FPS_DEFAULT)),
        'margem_bytes_quadro':frame_margin,
        'menor_margem_real':min_real_margin,
        'quadros_com_margem_relaxada':relaxed_margin_frames,
        'quantizacao_interna_maxima_permitida':MAX_INTERNAL_SQUASH,
        'compressao_estavel_por_combinacao':True,
        'audio_preservado_byte_a_byte':report['hash_audio_nao_video']==src.audio_hash,
    })
    report_path=out_path.with_name(out_path.stem+'_validacao.json')
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not report['ok']:
        raise ToolError('A validação final falhou: '+', '.join(report['problemas']))
    print('\nCONCLUÍDO')
    print(f'  saída: {out_path}')
    print(f'  quadros modificados: {modified}')
    print(f'  quadros MDEC validados: {report["quadros_mdec_decodificados"]}')
    print(f'  menor margem real: {min_real_margin} bytes')
    if relaxed_margin_frames:
        print(f'  margem adaptada em {relaxed_margin_frames} quadros excepcionais')
    print(f'  áudio/setores não-vídeo: IDÊNTICOS ({src.nonvideo_sectors} setores)')
    print(f'  validação: {report_path.name}')
    return report


def default_paths(root:Path,name:str) -> Tuple[Path,Path,Path]:
    stem=Path(name).stem.upper()
    if not stem.startswith('RU'): stem=name.upper()
    src=root/'extract'/f'{stem}.MOV'
    js=root/'videos'/f'{stem}.json'
    out=root/'zroup'/'mod'/f'{stem}.MOV'
    return src,js,out


def main(argv=None) -> int:
    ap=argparse.ArgumentParser(description='Monta RUxx.MOV legendado para o jogo, preservando áudio e tamanho.')
    ap.add_argument('mov',nargs='?',help='MOV original; ou nome RU01 usando extract/RU01.MOV e videos/RU01.json')
    ap.add_argument('json',nargs='?',help='JSON das legendas')
    ap.add_argument('-o','--saida',help='MOV de saída')
    ap.add_argument('--root',default='.',help='raiz do PS1-BR2 para modo RUxx')
    ap.add_argument('--force',action='store_true',help='substituir saída existente')
    ap.add_argument('--verificar',action='store_true',help='somente inspecionar o MOV')
    ap.add_argument('--todos',action='store_true',help='montar todos os RUxx com MOV em extract/ e JSON em videos/')
    args=ap.parse_args(argv)
    try:
        root=Path(args.root).expanduser().resolve()
        if args.todos:
            pasta_json=root/'videos'
            arquivos=sorted(pasta_json.glob('RU*.json'))
            if not arquivos: raise ToolError(f'Nenhum RUxx.json encontrado em {pasta_json}')
            feitos=0
            for js in arquivos:
                stem=js.stem.upper(); src=root/'extract'/f'{stem}.MOV'; out=root/'zroup'/'mod'/f'{stem}.MOV'
                if not src.exists():
                    print(f'PULANDO {stem}: original ausente em {src}')
                    continue
                print(f'\n===== {stem} =====')
                build_mov(src,js,out,args.force); feitos+=1
            if not feitos: raise ToolError('Nenhum vídeo pôde ser montado.')
            return 0
        if not args.mov:
            ap.print_help(); return 2
        if args.json:
            src=Path(args.mov).expanduser().resolve(); js=Path(args.json).expanduser().resolve()
            out=Path(args.saida).expanduser().resolve() if args.saida else src.with_name(src.stem+'_PTBR.MOV')
        elif args.mov.upper().startswith('RU') and not Path(args.mov).exists():
            src,js,out=default_paths(root,args.mov)
            if args.saida: out=Path(args.saida).expanduser().resolve()
        else:
            src=Path(args.mov).expanduser().resolve()
            if args.verificar:
                info=inspect_mov(src)
                print(json.dumps({'arquivo':src.name,'tamanho':src.stat().st_size,'quadros':len(info.frames),
                    'resolucao':[info.width,info.height],'str_version':info.version,
                    'setores_nao_video':info.nonvideo_sectors,'hash_audio_nao_video':info.audio_hash},ensure_ascii=False,indent=2))
                return 0
            raise ToolError('Informe também o JSON, ou use apenas RU01 dentro da estrutura padrão.')
        if not src.exists(): raise ToolError(f'Original não encontrado: {src}')
        if not js.exists(): raise ToolError(f'JSON não encontrado: {js}')
        build_mov(src,js,out,args.force)
        return 0
    except (ToolError,OSError,ValueError) as e:
        print(f'\nERRO: {e}',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
