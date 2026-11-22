"""Índice de busca (SQLite FTS5) sobre os repositórios de leis em Markdown."""
from __future__ import annotations

import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

ESQUEMA = 3
REPOS_DIR = Path(os.environ.get("REPOS_DIR", "/dados/repos"))
DB_PATH = Path(os.environ.get("DB_PATH", "/dados/indice.sqlite"))
MAX_CORPO = int(os.environ.get("MAX_CORPO_INDEXADO", str(2_000_000)))  # caracteres por norma

ESFERAS = {
    "federal": "federal", "estaduais": "estadual", "municipais": "municipal",
    "reguladores": "reguladores", "jurisprudencia": "jurisprudencia", "tratados": "tratados",
}
# LawCodex só tem legislação federal, em pastas próprias
LAWCODEX_DIRS = {"constituicao", "codigos", "decretos-lei", "emendas-constitucionais",
                 "leis-complementares", "leis-delegadas", "leis-imperio", "leis", "medidas-provisorias"}


TIPOS_EN = {"act": "Lei Ordinária", "complementary_law": "Lei Complementar", "decree_law": "Decreto-Lei",
            "constitutional_amendment": "Emenda Constitucional", "provisional_measure": "Medida Provisória",
            "delegated_law": "Lei Delegada", "constitution": "Constituição", "code": "Código"}


def conectar(somente_leitura: bool = False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if somente_leitura:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    else:
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def criar_esquema(con: sqlite3.Connection) -> None:
    con.executescript(f"""
    CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT);
    CREATE TABLE IF NOT EXISTS normas (
        id INTEGER PRIMARY KEY,
        caminho TEXT UNIQUE NOT NULL,
        repo TEXT, esfera TEXT, uf TEXT, municipio TEXT,
        tipo TEXT, numero TEXT, ano INTEGER, data TEXT,
        titulo TEXT, ementa TEXT, revogada INTEGER, fonte TEXT, tamanho INTEGER
    );
    CREATE INDEX IF NOT EXISTS ix_num ON normas(numero, ano);
    CREATE INDEX IF NOT EXISTS ix_esf ON normas(esfera, uf);
    CREATE VIRTUAL TABLE IF NOT EXISTS busca USING fts5(
        titulo, ementa, corpo,
        content='', contentless_delete=1,
        tokenize='unicode61 remove_diacritics 2'
    );
    INSERT OR IGNORE INTO meta VALUES ('esquema', '{ESQUEMA}');
    """)
    con.commit()


def meta(con, chave, valor=None):
    if valor is None:
        r = con.execute("SELECT valor FROM meta WHERE chave=?", (chave,)).fetchone()
        return r[0] if r else None
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (chave, str(valor)))
    con.commit()


def caminho_indexavel(rel: str) -> bool:
    """rel = 'leis/federal/...md' ou 'lawcodex/leis/...md'."""
    if not rel.endswith(".md"):
        return False
    partes = rel.split("/")
    repo, resto = partes[0], partes[1:]
    if any(p.startswith(("_", ".")) for p in resto):
        return False
    if repo == "leis":
        if len(resto) == 1:
            return resto[0] == "constituicao.md"
        return resto[0] in ESFERAS
    if repo == "lawcodex":
        return len(resto) > 1 and resto[0] in LAWCODEX_DIRS
    return False


_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _campo(fm: str, *chaves: str) -> str:
    for c in chaves:
        m = re.search(rf'^{c}:\s*"?(.*?)"?\s*$', fm, re.M)
        if m and m.group(1) not in ("", "null"):
            return m.group(1)
    return ""


def ler_registro(rel: str, texto: str) -> dict:
    m = _FM.match(texto)
    fm = m.group(1) if m else ""
    corpo = texto[m.end():] if m else texto
    partes = rel.split("/")
    repo = partes[0]
    if repo == "leis":
        top = partes[1]
        esfera = "federal" if top == "constituicao.md" else ESFERAS.get(top, top)
        uf = partes[2] if esfera in ("estadual", "municipal") and len(partes) > 3 else ""
        municipio = partes[3] if esfera == "municipal" and len(partes) > 4 else ""
    else:
        esfera, uf, municipio = "federal", "", ""
    titulo = _campo(fm, "titulo", "title")
    if not titulo:
        h1 = re.search(r"^#\s+(.+)$", corpo, re.M)
        titulo = h1.group(1).strip() if h1 else Path(rel).stem
    data = _campo(fm, "data", "date")
    ano = None
    my = re.match(r"(\d{4})", data) or re.search(r"-(\d{4})(?:-\d\d-\d\d)?\.md$", rel)
    if my:
        ano = int(my.group(1))
    status = _campo(fm, "revogada", "status").lower()
    revogada = 1 if status in ("true", "sim", "repealed", "revogada") else 0
    tipo = _campo(fm, "tipo", "type")
    tipo = TIPOS_EN.get(tipo, tipo)
    return dict(
        caminho=rel, repo=repo, esfera=esfera, uf=uf, municipio=municipio,
        tipo=tipo, numero=_campo(fm, "numero", "number"),
        ano=ano, data=data, titulo=titulo, ementa=_campo(fm, "ementa", "summary"),
        revogada=revogada, fonte=_campo(fm, "fonte", "source"), corpo=corpo[:MAX_CORPO],
    )


def _apagar(con, rel: str) -> None:
    r = con.execute("SELECT id FROM normas WHERE caminho=?", (rel,)).fetchone()
    if r:
        con.execute("DELETE FROM busca WHERE rowid=?", (r[0],))
        con.execute("DELETE FROM normas WHERE id=?", (r[0],))


def indexar_arquivo(con, rel: str) -> None:
    _apagar(con, rel)
    p = REPOS_DIR / rel
    if not p.is_file() or not caminho_indexavel(rel):
        return
    texto = p.read_text(encoding="utf-8", errors="replace")
    reg = ler_registro(rel, texto)
    cur = con.execute(
        """INSERT INTO normas(caminho, repo, esfera, uf, municipio, tipo, numero, ano, data,
                              titulo, ementa, revogada, fonte, tamanho)
           VALUES (:caminho,:repo,:esfera,:uf,:municipio,:tipo,:numero,:ano,:data,
                   :titulo,:ementa,:revogada,:fonte,:tam)""",
        {**reg, "tam": len(texto)})
    con.execute("INSERT INTO busca(rowid, titulo, ementa, corpo) VALUES (?,?,?,?)",
                (cur.lastrowid, reg["titulo"], reg["ementa"], reg["corpo"]))


def listar_todos() -> list[str]:
    out = []
    for repo in ("leis", "lawcodex"):
        base = REPOS_DIR / repo
        if not base.is_dir():
            continue
        for raiz, dirs, arqs in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
            for a in arqs:
                rel = os.path.relpath(os.path.join(raiz, a), REPOS_DIR).replace(os.sep, "/")
                if caminho_indexavel(rel):
                    out.append(rel)
    return out


def indexar(caminhos: list[str], progresso=None, lote: int = 2000) -> int:
    con = conectar()
    criar_esquema(con)
    n = 0
    t0 = time.time()
    for i, rel in enumerate(caminhos, 1):
        try:
            indexar_arquivo(con, rel)
            n += 1
        except Exception as e:  # um arquivo ruim não derruba o lote
            print(f"[indice] erro em {rel}: {e}", flush=True)
        if i % lote == 0:
            con.commit()
            if progresso:
                progresso(i, len(caminhos), time.time() - t0)
    con.commit()
    if progresso:
        progresso(len(caminhos), len(caminhos), time.time() - t0)
    con.close()
    return n


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()
