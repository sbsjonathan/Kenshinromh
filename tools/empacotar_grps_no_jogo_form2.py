#!/usr/bin/env python3
"""Insere SCPS_100.48, SYSTEM.GRP, ZROUP00..ZROUP99.GRP e RU00..RU13.MOV no BIN original.

Estrutura esperada quando este arquivo fica dentro de ``tools``::

    PS1-BR2/
      kenshin.bin
      tools/empacotar_grps_no_jogo.py
      zroup/mod/SYSTEM.GRP
      zroup/mod/ZROUP40.GRP
      zroup/mod/SCPS_100.48
      zroup/mod/RU12.MOV
      out/                         (criada automaticamente)

O programa examina apenas os arquivos reconhecidos existentes em zroup/mod.
O BIN original nunca e alterado; a saida padrao e out/kenshin_mod.bin.

Uso no a-Shell:

    cd ~/Documents/PS1-BR2
    python3 tools/empacotar_grps_no_jogo.py

Para conferir o plano sem criar o BIN:

    python3 tools/empacotar_grps_no_jogo.py --dry-run
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import sys
from typing import BinaryIO, Iterator


SYNC = bytes.fromhex("00FFFFFFFFFFFFFFFFFFFF00")
USER_SIZE = 2048
RAW_SECTOR_SIZE = 2352
RAW_USER_OFFSET = 24
VIDEO_MAGIC = 0x80010160
RECOGNIZED = re.compile(
    r"^(?:SCPS_100\.48|SYSTEM\.GRP|ZROUP(?:[0-9]{2})\.GRP|RU(?:0[0-9]|1[0-3])\.MOV)$",
    re.I,
)


class BuildError(Exception):
    pass


@dataclass
class ExtentStats:
    written: int = 0
    skipped_identical: int = 0
    form1_written: int = 0
    form2_written: int = 0

    def add(self, other: "ExtentStats") -> None:
        self.written += other.written
        self.skipped_identical += other.skipped_identical
        self.form1_written += other.form1_written
        self.form2_written += other.form2_written


@dataclass(frozen=True)
class DiscFormat:
    name: str
    sector_size: int
    user_offset: int
    raw: bool


RAW2352 = DiscFormat("RAW Mode 2/2352", RAW_SECTOR_SIZE, RAW_USER_OFFSET, True)
ISO2048 = DiscFormat("ISO/2048", USER_SIZE, 0, False)


@dataclass(frozen=True)
class IsoEntry:
    path: str
    lba: int
    size: int
    flags: int
    file_unit: int
    interleave: int
    record_lba: int | None
    record_offset: int | None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def detect_format(f: BinaryIO, total_size: int) -> DiscFormat:
    f.seek(0)
    beginning = f.read(32)
    if total_size % RAW_SECTOR_SIZE == 0 and beginning[:12] == SYNC:
        if len(beginning) < 24 or beginning[15] != 2:
            raise BuildError("O BIN RAW nao esta em Mode 2/2352.")
        return RAW2352
    if total_size % USER_SIZE == 0:
        return ISO2048
    raise BuildError("Formato desconhecido: nao e RAW/2352 nem ISO/2048.")


class Iso9660:
    def __init__(self, f: BinaryIO, fmt: DiscFormat, total_size: int):
        self.f = f
        self.fmt = fmt
        self.physical_sectors = total_size // fmt.sector_size
        self.bias = self._find_pvd_bias()
        self.pvd = self.read_block(16)
        block_le = int.from_bytes(self.pvd[128:130], "little")
        block_be = int.from_bytes(self.pvd[130:132], "big")
        if block_le != USER_SIZE or block_be != USER_SIZE:
            raise BuildError("O ISO nao usa blocos logicos de 2048 bytes.")
        self.root = self._parse_record(self.pvd, 156, "/", None, None)

    def _read_physical_user(self, physical: int) -> bytes:
        if not 0 <= physical < self.physical_sectors:
            raise BuildError(f"Setor fisico fora da imagem: {physical}")
        self.f.seek(physical * self.fmt.sector_size)
        sector = self.f.read(self.fmt.sector_size)
        if len(sector) != self.fmt.sector_size:
            raise BuildError(f"Leitura incompleta no setor {physical}")
        if self.fmt.raw:
            # ISO9660 usa blocos logicos de 2048 bytes. Nos filmes, esses
            # blocos podem estar dentro de setores XA Mode 2 Form 2 de 2324
            # bytes. Para a visao ISO, lemos somente os primeiros 2048.
            raw_mode2_form(sector, physical)
        start = self.fmt.user_offset
        return sector[start : start + USER_SIZE]

    def _find_pvd_bias(self) -> int:
        for physical in range(16, min(self.physical_sectors, 616)):
            try:
                data = self._read_physical_user(physical)
            except BuildError:
                continue
            if data[0] == 1 and data[1:6] == b"CD001" and data[6] == 1:
                return physical - 16
        raise BuildError("Tabela ISO9660 CD001 nao encontrada em kenshin.bin.")

    def read_block(self, lba: int) -> bytes:
        return self._read_physical_user(self.bias + lba)

    def read_extent(self, lba: int, size: int) -> bytes:
        parts = [self.read_block(lba + i) for i in range((size + 2047) // 2048)]
        return b"".join(parts)[:size]

    @staticmethod
    def _parse_record(
        data: bytes,
        pos: int,
        path: str,
        record_lba: int | None,
        record_offset: int | None,
    ) -> IsoEntry:
        if pos >= len(data) or data[pos] < 34:
            raise BuildError(f"Registro ISO invalido em {path}")
        length = data[pos]
        record = data[pos : pos + length]
        if len(record) != length:
            raise BuildError(f"Registro ISO truncado em {path}")
        lba_le = int.from_bytes(record[2:6], "little")
        lba_be = int.from_bytes(record[6:10], "big")
        size_le = int.from_bytes(record[10:14], "little")
        size_be = int.from_bytes(record[14:18], "big")
        if lba_le != lba_be or size_le != size_be:
            raise BuildError(f"Copias LE/BE divergentes no registro {path}")
        return IsoEntry(
            path, lba_le, size_le, record[25], record[26], record[27],
            record_lba, record_offset,
        )

    def _iter_directory(self, directory: IsoEntry) -> Iterator[IsoEntry]:
        data = self.read_extent(directory.lba, directory.size)
        pos = 0
        while pos < len(data):
            length = data[pos]
            if length == 0:
                pos = ((pos // USER_SIZE) + 1) * USER_SIZE
                continue
            if length < 34 or pos + length > len(data):
                raise BuildError(f"Diretorio ISO corrompido: {directory.path}")
            record = data[pos : pos + length]
            name_raw = record[33 : 33 + record[32]]
            current = pos
            pos += length
            if name_raw in (b"\x00", b"\x01"):
                continue
            name = name_raw.decode("ascii", errors="strict").split(";", 1)[0]
            path = directory.path.rstrip("/") + "/" + name
            yield self._parse_record(
                record,
                0,
                path,
                directory.lba + current // USER_SIZE,
                current % USER_SIZE,
            )

    def all_files(self) -> list[IsoEntry]:
        files: list[IsoEntry] = []
        visited: set[tuple[int, int]] = set()

        def visit(directory: IsoEntry, depth: int) -> None:
            if depth > 32:
                raise BuildError("Arvore ISO profunda demais.")
            key = (directory.lba, directory.size)
            if key in visited:
                return
            visited.add(key)
            for entry in self._iter_directory(directory):
                if entry.flags & 2:
                    visit(entry, depth + 1)
                else:
                    files.append(entry)

        visit(self.root, 0)
        return files


def raw_mode2_form(sector: bytes, physical: int) -> int:
    """Valida um setor RAW Mode 2 e retorna 1 ou 2 conforme o XA Form."""
    if len(sector) != RAW_SECTOR_SIZE or sector[:12] != SYNC or sector[15] != 2:
        raise BuildError(f"Setor {physical} nao e RAW Mode 2 valido.")
    if sector[16:20] != sector[20:24]:
        raise BuildError(f"Subcabecalhos XA divergentes no setor {physical}.")
    return 2 if (sector[18] & 0x20) else 1


def validate_raw_form1(sector: bytes, physical: int) -> None:
    if raw_mode2_form(sector, physical) != 1:
        raise BuildError(f"Setor {physical} e Mode 2 Form 2; esperado Form 1.")


def make_ecc_tables() -> tuple[list[int], list[int], list[int]]:
    forward = [0] * 256
    backward = [0] * 256
    edc = [0] * 256
    for i in range(256):
        value = i << 1
        if value & 0x100:
            value ^= 0x11D
        forward[i] = value
        backward[i ^ value] = i

        value = i
        for _ in range(8):
            value = (value >> 1) ^ (0xD8018001 if value & 1 else 0)
        edc[i] = value & 0xFFFFFFFF
    return forward, backward, edc


ECC_F, ECC_B, EDC_LUT = make_ecc_tables()


def compute_edc(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value >> 8) ^ EDC_LUT[(value ^ byte) & 0xFF]
    return value & 0xFFFFFFFF


def compute_ecc(
    source: bytes,
    major_count: int,
    minor_count: int,
    major_mult: int,
    minor_inc: int,
) -> bytes:
    size = major_count * minor_count
    if len(source) < size:
        raise BuildError("Fonte curta durante calculo ECC.")
    result = bytearray(major_count * 2)
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        ecc_a = 0
        ecc_b = 0
        for _ in range(minor_count):
            temp = source[index]
            index += minor_inc
            if index >= size:
                index -= size
            ecc_a ^= temp
            ecc_b ^= temp
            ecc_a = ECC_F[ecc_a]
        ecc_a = ECC_B[ECC_F[ecc_a] ^ ecc_b]
        result[major] = ecc_a
        result[major + major_count] = ecc_a ^ ecc_b
    return bytes(result)


def rebuild_raw_form1(sector: bytes) -> bytes:
    output = bytearray(sector)
    validate_raw_form1(output, -1)
    struct.pack_into("<I", output, 2072, compute_edc(output[16:2072]))
    saved_header = bytes(output[12:16])
    output[12:16] = b"\0\0\0\0"
    output[2076:2248] = compute_ecc(output[12:2076], 86, 24, 2, 86)
    output[2248:2352] = compute_ecc(output[12:2248], 52, 43, 86, 88)
    output[12:16] = saved_header
    return bytes(output)




def rebuild_raw_form2(sector: bytes) -> bytes:
    """Recalcula somente o EDC de um setor XA Mode 2 Form 2.

    Os 2324 bytes de dados ficam em 24..2347 e o EDC em 2348..2351.
    Nao existe ECC em Form 2.
    """
    output = bytearray(sector)
    if raw_mode2_form(output, -1) != 2:
        raise BuildError("Tentativa de reconstruir como Form 2 um setor que nao e Form 2.")
    struct.pack_into("<I", output, 2348, compute_edc(output[16:2348]))
    return bytes(output)

def verify_ecc_algorithm(f: BinaryIO, iso: Iso9660) -> None:
    if not iso.fmt.raw:
        return
    physical = iso.bias + 16
    f.seek(physical * RAW_SECTOR_SIZE)
    original = f.read(RAW_SECTOR_SIZE)
    rebuilt = rebuild_raw_form1(original)
    if rebuilt[2072:] != original[2072:]:
        raise BuildError(
            "A verificacao EDC/ECC do setor original falhou; o BIN nao sera alterado."
        )


def scan_mods(folder: Path) -> tuple[list[tuple[str, Path]], list[Path]]:
    if not folder.is_dir():
        raise BuildError(f"Pasta de modificados nao encontrada: {folder}")
    found: dict[str, Path] = {}
    ignored: list[Path] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        name = path.name.upper()
        if not RECOGNIZED.fullmatch(name):
            ignored.append(path)
            continue
        if name in found:
            raise BuildError(f"Arquivo duplicado para {name}: {found[name]} e {path}")
        found[name] = path

    def order(item: tuple[str, Path]) -> tuple[int, int]:
        name = item[0]
        if name == "SCPS_100.48":
            return (0, 0)
        if name == "SYSTEM.GRP":
            return (1, 0)
        if name.startswith("ZROUP"):
            return (2, int(name[5:7]))
        return (3, int(name[2:4]))

    return sorted(found.items(), key=order), ignored


def map_disc_entries(files: list[IsoEntry]) -> dict[str, IsoEntry]:
    result: dict[str, IsoEntry] = {}
    duplicates: set[str] = set()
    for entry in files:
        name = entry.path.rsplit("/", 1)[-1].upper()
        if name in result:
            duplicates.add(name)
        else:
            result[name] = entry
    for name in duplicates:
        result.pop(name, None)
    return result


def copy_with_progress(source: Path, target: Path) -> None:
    total = source.stat().st_size
    copied = 0
    next_report = 64 * 1024 * 1024
    with source.open("rb") as src, target.open("wb") as dst:
        while chunk := src.read(8 * 1024 * 1024):
            dst.write(chunk)
            copied += len(chunk)
            if copied >= next_report or copied == total:
                percent = copied * 100 / total
                print(f"  copia: {percent:5.1f}%", flush=True)
                next_report += 64 * 1024 * 1024
        dst.flush()
        os.fsync(dst.fileno())


def patch_user_block(f: BinaryIO, iso: Iso9660, lba: int, payload: bytes) -> bool:
    """Grava um bloco somente quando seus 2048 bytes realmente mudaram.

    Retorna ``True`` quando houve escrita. Em filmes XA, isto mantém cada
    setor de áudio idêntico nos 2352 bytes físicos, inclusive EDC e campos que
    não aparecem na visão ISO.
    """
    if len(payload) != USER_SIZE:
        raise BuildError("Bloco de usuario deve ter 2048 bytes.")
    physical = iso.bias + lba
    offset = physical * iso.fmt.sector_size
    f.seek(offset)
    sector = f.read(iso.fmt.sector_size)
    if len(sector) != iso.fmt.sector_size:
        raise BuildError(f"Setor truncado ao escrever LBA {lba}")
    if iso.fmt.raw:
        form = raw_mode2_form(sector, physical)
        current = sector[RAW_USER_OFFSET : RAW_USER_OFFSET + USER_SIZE]
        if current == payload:
            return False
        changed = bytearray(sector)
        # Substitui apenas o bloco logico ISO de 2048 bytes.
        # Em Form 2, preserva os 276 bytes adicionais (2072..2347),
        # importantes para os setores XA de audio/video.
        changed[RAW_USER_OFFSET : RAW_USER_OFFSET + USER_SIZE] = payload
        if form == 1:
            sector = rebuild_raw_form1(bytes(changed))
        else:
            sector = rebuild_raw_form2(bytes(changed))
    else:
        if sector == payload:
            return False
        sector = payload
    f.seek(offset)
    f.write(sector)
    return True


def write_extent(f: BinaryIO, iso: Iso9660, entry: IsoEntry, content: bytes) -> ExtentStats:
    sectors = (len(content) + USER_SIZE - 1) // USER_SIZE
    original_sectors = (entry.size + USER_SIZE - 1) // USER_SIZE
    if sectors > original_sectors:
        raise BuildError(
            f"{entry.path}: modificado usa {sectors} setores, original usa "
            f"{original_sectors}. Esta versao segura nao realoca arquivos maiores."
        )
    stats = ExtentStats()
    for index in range(sectors):
        chunk = content[index * USER_SIZE : (index + 1) * USER_SIZE]
        payload = chunk.ljust(USER_SIZE, b"\0")
        lba = entry.lba + index
        if iso.fmt.raw:
            physical = iso.bias + lba
            f.seek(physical * RAW_SECTOR_SIZE)
            before = f.read(RAW_SECTOR_SIZE)
            form = raw_mode2_form(before, physical)
        else:
            form = 0
        if patch_user_block(f, iso, lba, payload):
            stats.written += 1
            if form == 1:
                stats.form1_written += 1
            elif form == 2:
                stats.form2_written += 1
        else:
            stats.skipped_identical += 1
    return stats


def validate_movie_replacement(iso: Iso9660, entry: IsoEntry, path: Path) -> tuple[int, int]:
    """Recusa RUxx.MOV que altere áudio ou a estrutura de setores.

    Os setores STR começam com ``VIDEO_MAGIC``. Tudo que não é vídeo é
    preservado byte a byte; no jogo esses blocos são o fluxo CD-XA de áudio.
    """
    if path.stat().st_size != entry.size:
        raise BuildError(
            f"{path.name}: filme precisa manter exatamente {entry.size} bytes; "
            f"recebido {path.stat().st_size}."
        )
    if entry.size % USER_SIZE:
        raise BuildError(f"{path.name}: tamanho do filme nao e multiplo de 2048.")
    video = audio = 0
    with path.open("rb") as modified:
        for index in range(entry.size // USER_SIZE):
            before = iso.read_block(entry.lba + index)
            after = modified.read(USER_SIZE)
            if len(after) != USER_SIZE:
                raise BuildError(f"{path.name}: setor {index} truncado.")
            before_video = struct.unpack_from("<I", before, 0)[0] == VIDEO_MAGIC
            after_video = struct.unpack_from("<I", after, 0)[0] == VIDEO_MAGIC
            if before_video != after_video:
                raise BuildError(f"{path.name}: tipo do setor {index} mudou.")
            if before_video:
                video += 1
            else:
                audio += 1
                if before != after:
                    raise BuildError(
                        f"{path.name}: setor XA de audio {index} foi alterado. "
                        "O empacotamento foi cancelado."
                    )
    return video, audio


def read_physical_sector(f: BinaryIO, iso: Iso9660, lba: int) -> bytes:
    physical = iso.bias + lba
    f.seek(physical * iso.fmt.sector_size)
    sector = f.read(iso.fmt.sector_size)
    if len(sector) != iso.fmt.sector_size:
        raise BuildError(f"Setor fisico truncado no LBA {lba}.")
    return sector


def verify_extent_physical(
    source_f: BinaryIO,
    source_iso: Iso9660,
    final_f: BinaryIO,
    final_iso: Iso9660,
    original_entry: IsoEntry,
    final_entry: IsoEntry,
    content: bytes,
) -> ExtentStats:
    """Verifica dados lógicos e, em RAW, todos os 2352 bytes físicos."""
    stats = ExtentStats()
    sectors = (len(content) + USER_SIZE - 1) // USER_SIZE
    for index in range(sectors):
        expected = content[index * USER_SIZE : (index + 1) * USER_SIZE].ljust(
            USER_SIZE, b"\0"
        )
        source_lba = original_entry.lba + index
        final_lba = final_entry.lba + index
        original_user = source_iso.read_block(source_lba)
        final_user = final_iso.read_block(final_lba)
        if final_user != expected:
            raise BuildError(
                f"Verificacao logica falhou em {final_entry.path}, setor {index}."
            )
        original_sector = read_physical_sector(source_f, source_iso, source_lba)
        final_sector = read_physical_sector(final_f, final_iso, final_lba)
        if original_user == expected:
            stats.skipped_identical += 1
            if original_sector != final_sector:
                raise BuildError(
                    f"Setor inalterado de {final_entry.path}, indice {index}, "
                    "nao permaneceu identico fisicamente."
                )
            continue

        stats.written += 1
        if not final_iso.fmt.raw:
            continue
        if original_sector[:24] != final_sector[:24]:
            raise BuildError(
                f"Cabecalho/subcabecalho XA mudou em {final_entry.path}, setor {index}."
            )
        original_form = raw_mode2_form(original_sector, source_iso.bias + source_lba)
        final_form = raw_mode2_form(final_sector, final_iso.bias + final_lba)
        if original_form != final_form:
            raise BuildError(f"XA Form mudou em {final_entry.path}, setor {index}.")
        if final_form == 1:
            stats.form1_written += 1
            if rebuild_raw_form1(final_sector) != final_sector:
                raise BuildError(f"EDC/ECC Form 1 invalido em {final_entry.path}, setor {index}.")
        else:
            stats.form2_written += 1
            if original_sector[2072:2348] != final_sector[2072:2348]:
                raise BuildError(
                    f"Cauda XA Form 2 mudou em {final_entry.path}, setor {index}."
                )
            if rebuild_raw_form2(final_sector) != final_sector:
                raise BuildError(f"EDC Form 2 invalido em {final_entry.path}, setor {index}.")
    return stats


def update_directory_record(
    f: BinaryIO, iso: Iso9660, entry: IsoEntry, new_size: int
) -> None:
    if entry.record_lba is None or entry.record_offset is None:
        raise BuildError(f"Local do registro ISO ausente para {entry.path}")
    payload = bytearray(iso.read_block(entry.record_lba))
    pos = entry.record_offset
    if pos + 18 > USER_SIZE or payload[pos] < 34:
        raise BuildError(f"Registro ISO final invalido para {entry.path}")
    struct.pack_into("<I", payload, pos + 10, new_size)
    struct.pack_into(">I", payload, pos + 14, new_size)
    patch_user_block(f, iso, entry.record_lba, bytes(payload))


def validate_entry(entry: IsoEntry, mod_size: int) -> None:
    if entry.flags & 0x80:
        raise BuildError(f"{entry.path} usa multiplos extents, nao suportado.")
    if entry.file_unit or entry.interleave:
        raise BuildError(f"{entry.path} e intercalado, nao suportado.")
    capacity = ((entry.size + USER_SIZE - 1) // USER_SIZE) * USER_SIZE
    if mod_size > capacity:
        raise BuildError(
            f"{entry.path}: {mod_size} bytes nao cabem nos {capacity} bytes reservados."
        )


def build(args: argparse.Namespace) -> None:
    source = args.bin.expanduser().resolve()
    mods_folder = args.mods.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        raise BuildError(f"BIN original nao encontrado: {source}")
    mods, ignored = scan_mods(mods_folder)
    if not mods:
        raise BuildError(
            "Nenhum SCPS_100.48, SYSTEM.GRP, ZROUP00..99.GRP ou "
            "RU00..RU13.MOV encontrado em zroup/mod."
        )

    with source.open("rb") as f:
        fmt = detect_format(f, source.stat().st_size)
        iso = Iso9660(f, fmt, source.stat().st_size)
        verify_ecc_algorithm(f, iso)
        disc = map_disc_entries(iso.all_files())
        plan: list[tuple[str, Path, IsoEntry]] = []
        movie_audits: dict[str, tuple[int, int]] = {}
        for name, path in mods:
            if name not in disc:
                raise BuildError(f"{name} nao foi encontrado de forma unica no disco.")
            entry = disc[name]
            validate_entry(entry, path.stat().st_size)
            if re.fullmatch(r"RU(?:0[0-9]|1[0-3])\.MOV", name, re.I):
                movie_audits[name] = validate_movie_replacement(iso, entry, path)
            plan.append((name, path, entry))

    print("ARQUIVOS QUE SERAO EMPACOTADOS:")
    for name, path, entry in plan:
        detail = ""
        if name in movie_audits:
            video, audio = movie_audits[name]
            detail = f"; filme auditado: {video} video + {audio} audio intactos"
        print(
            f"  {name}: {entry.size} -> {path.stat().st_size} bytes; "
            f"LBA {entry.lba}{detail}"
        )
    if ignored:
        print("\nIgnorados por nao serem SCPS_100.48/SYSTEM/ZROUP00-99/RU00-13.MOV:")
        for path in ignored:
            print(f"  {path.name}")
    if args.dry_run:
        print("\nDRY-RUN CONCLUIDO: nenhum arquivo foi escrito.")
        return

    if output.exists() and not args.force:
        raise BuildError(f"A saida ja existe: {output}\nUse --force para substituir.")
    output.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output.parent).free
    if free < source.stat().st_size + 64 * 1024 * 1024:
        raise BuildError("Espaco livre insuficiente para copiar o BIN com seguranca.")
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()

    try:
        print(f"\nCopiando o BIN original para {output.name}...", flush=True)
        copy_with_progress(source, temporary)
        written_total = ExtentStats()
        with temporary.open("r+b") as f:
            iso = Iso9660(f, fmt, temporary.stat().st_size)
            for name, path, entry in plan:
                content = path.read_bytes()
                print(f"  inserindo {name}...", flush=True)
                stats = write_extent(f, iso, entry, content)
                written_total.add(stats)
                print(
                    f"    gravados: {stats.written}; "
                    f"identicos preservados: {stats.skipped_identical}"
                )
                if len(content) != entry.size:
                    # Reabre a visao ISO apos cada alteracao de diretorio.
                    update_directory_record(f, iso, entry, len(content))
                    iso = Iso9660(f, fmt, temporary.stat().st_size)
            f.flush()
            os.fsync(f.fileno())

        # Verificacao independente. Para RAW/2352, compara tambem cabecalhos,
        # subcabecalhos, cauda XA, EDC/ECC e a identidade fisica de cada bloco
        # que nao deveria ter mudado.
        verified_total = ExtentStats()
        with source.open("rb") as source_f, temporary.open("rb") as final_f:
            source_iso = Iso9660(source_f, fmt, source.stat().st_size)
            final_iso = Iso9660(final_f, fmt, temporary.stat().st_size)
            final_entries = map_disc_entries(final_iso.all_files())
            for name, path, original_entry in plan:
                final = final_entries.get(name)
                content = path.read_bytes()
                if final is None or final_iso.read_extent(final.lba, final.size) != content:
                    raise BuildError(f"Verificacao final falhou para {name}.")
                checked = verify_extent_physical(
                    source_f, source_iso, final_f, final_iso,
                    original_entry, final, content,
                )
                verified_total.add(checked)
                print(
                    f"  verificado {name}: {checked.written} alterados; "
                    f"{checked.skipped_identical} fisicamente identicos"
                )
        os.replace(temporary, output)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    print("\nJOGO EMPACOTADO E VERIFICADO")
    print(f"  saida:    {output}")
    print(f"  tamanho:  {output.stat().st_size} bytes")
    print(f"  arquivos: {len(plan)}")
    print(f"  setores gravados: {written_total.written}")
    print(f"  setores identicos nao tocados: {written_total.skipped_identical}")
    if fmt.raw:
        print(
            f"  RAW verificado: Form1={verified_total.form1_written}; "
            f"Form2={verified_total.form2_written}"
        )
    if args.hash:
        print(f"  SHA-256:  {sha256_file(output)}")


def parse_args() -> argparse.Namespace:
    script_folder = Path(__file__).resolve().parent
    project = script_folder.parent if script_folder.name.lower() == "tools" else script_folder
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bin", type=Path, default=project / "kenshin.bin")
    p.add_argument("--mods", type=Path, default=project / "zroup" / "mod")
    p.add_argument("--output", type=Path, default=project / "out" / "kenshin_mod.bin")
    p.add_argument("--dry-run", action="store_true", help="mostra o plano sem copiar o BIN")
    p.add_argument("--force", action="store_true", help="substitui uma saida existente")
    p.add_argument("--hash", action="store_true", help="calcula SHA-256 do BIN final")
    return p.parse_args()


def main() -> int:
    try:
        build(parse_args())
        return 0
    except BuildError as exc:
        print(f"\nERRO: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
