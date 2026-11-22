"""Baixa os repositórios de leis, mantém em dia e atualiza o índice."""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

import indice

REPOS = {
    "leis": "https://github.com/ctresb/leis.git",
    "lawcodex": "https://github.com/LawCodex/br.git",
}
# Fica de fora: originais (HTML/PDF), índices internos, scripts e imagens.
SPARSE = ["/*", "!_*/", "!/fonte/", "!/scripts/", "!/assets/"]
HORA = os.environ.get("HORA_ATUALIZACAO", "04:30")
TZ = ZoneInfo(os.environ.get("TZ", "America/Sao_Paulo"))

estado = {"fase": "iniciando", "detalhe": "", "ultima_atualizacao": None, "erro": None}


def git(*args, cwd=None, timeout=6 * 3600) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[-500:]}")
    return r.stdout


def clonar_se_preciso(nome: str, url: str) -> bool:
    destino = indice.REPOS_DIR / nome
    if (destino / ".git").is_dir():
        return False
    indice.REPOS_DIR.mkdir(parents=True, exist_ok=True)
    estado.update(fase="baixando", detalhe=f"clonando {nome} (pode levar de 30 min a algumas horas)")
    git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", url, str(destino))
    git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=destino)
    git("checkout", cwd=destino)
    return True


def puxar(nome: str) -> list[str]:
    """Atualiza o repositório e devolve os caminhos (relativos a REPOS_DIR) que mudaram."""
    destino = indice.REPOS_DIR / nome
    antes = git("rev-parse", "HEAD", cwd=destino).strip()
    git("fetch", "--depth", "1", "origin", cwd=destino)
    depois = git("rev-parse", "FETCH_HEAD", cwd=destino).strip()
    if antes == depois:
        return []
    nomes = git("diff", "--name-only", antes, depois, cwd=destino).splitlines()
    git("reset", "--hard", depois, cwd=destino)
    return [f"{nome}/{n}" for n in nomes]


def _progresso(i, total, seg):
    ritmo = i / seg if seg else 0
    falta = (total - i) / ritmo / 60 if ritmo else 0
    estado["detalhe"] = f"{i:,}/{total:,} arquivos ({ritmo:,.0f}/s, faltam ~{falta:,.0f} min)".replace(",", ".")


def ciclo_inicial():
    novos = [clonar_se_preciso(n, u) for n, u in REPOS.items()]
    con = indice.conectar()
    indice.criar_esquema(con)
    feito = indice.meta(con, "indexado_em")
    versao = indice.meta(con, "esquema")
    con.close()
    if any(novos) or not feito or versao != str(indice.ESQUEMA):
        estado.update(fase="indexando", detalhe="listando arquivos")
        caminhos = indice.listar_todos()
        indice.indexar(caminhos, progresso=_progresso)
        con = indice.conectar()
        indice.meta(con, "indexado_em", dt.datetime.now(TZ).isoformat(timespec="minutes"))
        con.close()


def ciclo_diario():
    mudados = []
    for nome in REPOS:
        estado.update(fase="atualizando", detalhe=f"git fetch {nome}")
        mudados += puxar(nome)
    if mudados:
        estado.update(fase="indexando", detalhe=f"{len(mudados)} arquivos mudaram")
        indice.indexar(mudados, progresso=_progresso)
    con = indice.conectar()
    indice.meta(con, "indexado_em", dt.datetime.now(TZ).isoformat(timespec="minutes"))
    indice.meta(con, "ultima_mudanca", len(mudados))
    con.close()


def segundos_ate_proxima():
    h, m = map(int, HORA.split(":"))
    agora = dt.datetime.now(TZ)
    alvo = agora.replace(hour=h, minute=m, second=0, microsecond=0)
    if alvo <= agora:
        alvo += dt.timedelta(days=1)
    return (alvo - agora).total_seconds()


def laco():
    while True:
        try:
            ciclo_inicial()
            break
        except Exception as e:
            estado.update(fase="erro", erro=str(e))
            print(f"[atualizador] {e}", flush=True)
            time.sleep(600)
    estado.update(fase="pronto", detalhe="", erro=None,
                  ultima_atualizacao=dt.datetime.now(TZ).isoformat(timespec="minutes"))
    while True:
        time.sleep(segundos_ate_proxima())
        try:
            ciclo_diario()
            estado.update(fase="pronto", detalhe="", erro=None,
                          ultima_atualizacao=dt.datetime.now(TZ).isoformat(timespec="minutes"))
        except Exception as e:
            estado.update(fase="pronto", erro=f"atualização falhou: {e}")
            print(f"[atualizador] {e}", flush=True)


def iniciar():
    if os.environ.get("SEM_GIT") == "1":
        estado.update(fase="pronto")
        return
    threading.Thread(target=laco, daemon=True, name="atualizador").start()
