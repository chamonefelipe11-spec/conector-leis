"""Conector MCP "Leis do Brasil": busca e leitura de normas brasileiras em Markdown."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import atualizador
import indice

TOKEN = os.environ.get("TOKEN", "").strip()
if len(TOKEN) < 24:
    sys.exit("Defina TOKEN no .env com pelo menos 24 caracteres (ex.: openssl rand -hex 24).")

mcp = FastMCP(
    "Leis do Brasil",
    instructions=(
        "Acervo de normas brasileiras em Markdown (Constituição, leis federais, estaduais, "
        "municipais, atos de órgãos reguladores, súmulas e tratados). Use buscar_leis para achar "
        "normas por assunto, buscar_por_numero quando souber o número, e ler_norma para ler o texto "
        "(de preferência só o artigo que interessa). Cite sempre o artigo e o caminho/fonte. "
        "Normas com revogada=true não valem mais, salvo para fatos da época."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORTA", "8787")),
    streamable_http_path=f"/{TOKEN}/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_OPS = re.compile(r'"|\b(AND|OR|NOT|NEAR)\b|\*')


def _consulta_fts(q: str) -> str:
    q = q.strip()
    if _OPS.search(q):
        return q
    termos = re.findall(r"[\wº°§-]+", q)
    return " AND ".join(f'"{t}"' for t in termos if len(t) > 1)


def _trecho(caminho: str, consulta: str, largura: int = 420) -> tuple[str, str]:
    """Devolve (trecho, artigo) em volta do primeiro termo encontrado."""
    try:
        texto = (indice.REPOS_DIR / caminho).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    termos = [t for t in re.findall(r"[\w-]+", indice.sem_acento(consulta))
              if len(t) > 2 and t.upper() not in ("AND", "OR", "NOT", "NEAR")]
    base = indice.sem_acento(texto)
    corpo_ini = texto.find("\n---", 3) + 4 if texto.startswith("---") else 0
    pos = -1
    for t in sorted(termos, key=len, reverse=True):
        pos = base.find(t, corpo_ini)
        if pos >= 0:
            break
    if pos < 0:
        pos = corpo_ini
    ini = max(corpo_ini, pos - largura // 2)
    trecho = texto[ini: ini + largura].replace("\n", " ").strip()
    arts = list(re.finditer(r"Art\.?\s*(\d+[º°o]?(?:-[A-Z])?)", texto[:pos + 1]))
    artigo = f"Art. {arts[-1].group(1)}" if arts else ""
    return trecho, artigo


def _aviso_estado() -> str | None:
    e = atualizador.estado
    if e["fase"] != "pronto":
        return f"Acervo ainda em preparo: {e['fase']} — {e['detalhe']}. Resultados podem estar incompletos."
    return None


@mcp.tool()
def buscar_leis(consulta: str, esfera: str = "", uf: str = "", tipo: str = "",
                ano_de: int = 0, ano_ate: int = 0, incluir_revogadas: bool = False,
                limite: int = 10) -> dict:
    """Busca normas por assunto no acervo completo.

    consulta: palavras-chave (todas precisam aparecer). Aceita sintaxe FTS5:
      "frase exata", OR, NOT, NEAR(a b, 10), prefixo*. Acentos são ignorados.
    esfera: federal | estadual | municipal | reguladores | jurisprudencia | tratados (vazio = todas)
    uf: sigla do estado (MG, SP...) para esferas estadual e municipal.
    tipo: filtra pelo tipo da norma, ex. "Lei Complementar", "Decreto", "Súmula".
    incluir_revogadas: por padrão só normas em vigor.
    limite: até 50.
    Retorna caminho (use em ler_norma), título, ementa, trecho e o artigo onde o trecho está.
    """
    q = _consulta_fts(consulta)
    if not q:
        return {"erro": "consulta vazia"}
    filtros, args = ["busca MATCH ?"], [q]
    if esfera:
        filtros.append("n.esfera = ?"); args.append(esfera.lower())
    if uf:
        filtros.append("n.uf = ?"); args.append(uf.upper())
    if tipo:
        filtros.append("n.tipo LIKE ?"); args.append(f"%{tipo}%")
    if ano_de:
        filtros.append("n.ano >= ?"); args.append(ano_de)
    if ano_ate:
        filtros.append("n.ano <= ?"); args.append(ano_ate)
    if not incluir_revogadas:
        filtros.append("n.revogada = 0")
    limite = max(1, min(int(limite), 50))
    con = indice.conectar(somente_leitura=True)
    try:
        linhas = con.execute(
            f"""SELECT n.*, bm25(busca, 10.0, 5.0, 1.0) AS r FROM busca
                JOIN normas n ON n.id = busca.rowid
                WHERE {' AND '.join(filtros)} ORDER BY r LIMIT ?""",
            (*args, limite)).fetchall()
    except Exception as e:
        return {"erro": f"consulta inválida para FTS5: {e}", "consulta_usada": q}
    finally:
        con.close()
    res = []
    for l in linhas:
        trecho, artigo = _trecho(l["caminho"], consulta)
        res.append({
            "caminho": l["caminho"], "titulo": l["titulo"], "ementa": l["ementa"],
            "esfera": l["esfera"], "uf": l["uf"] or None, "municipio": l["municipio"] or None,
            "tipo": l["tipo"], "ano": l["ano"], "revogada": bool(l["revogada"]),
            "fonte_oficial": l["fonte"], "artigo_do_trecho": artigo or None, "trecho": trecho,
        })
    out = {"consulta_usada": q, "resultados": res}
    if (a := _aviso_estado()):
        out["aviso"] = a
    return out


@mcp.tool()
def buscar_por_numero(numero: str, ano: int = 0, tipo: str = "", esfera: str = "",
                      uf: str = "") -> dict:
    """Acha uma norma pelo número (ex.: numero="14133" ou "14.133", ano=2021, tipo="Lei").
    Federal costuma ter duas cópias: 'leis/...' (ctresb) e 'lawcodex/...' (texto do Planalto)."""
    num = re.sub(r"[^\dA-Za-z-]", "", numero)
    filtros, args = ["replace(n.numero, '.', '') = ?"], [num]
    if ano:
        filtros.append("n.ano = ?"); args.append(ano)
    if tipo:
        filtros.append("n.tipo LIKE ?"); args.append(f"%{tipo}%")
    if esfera:
        filtros.append("n.esfera = ?"); args.append(esfera.lower())
    if uf:
        filtros.append("n.uf = ?"); args.append(uf.upper())
    con = indice.conectar(somente_leitura=True)
    linhas = con.execute(f"SELECT * FROM normas n WHERE {' AND '.join(filtros)} LIMIT 30", args).fetchall()
    if esfera in ("", "federal") and not uf:
        # LawCodex não traz 'numero' no cabeçalho: procura pelo nome do arquivo
        padrao = f"lawcodex/%-{num}-{ano}%" if ano else f"lawcodex/%-{num}-%"
        extra = con.execute("SELECT * FROM normas WHERE caminho LIKE ? AND (? = '' OR tipo LIKE ?) LIMIT 10",
                            (padrao, tipo, f"%{tipo}%")).fetchall()
        linhas = list(linhas) + [e for e in extra if e["caminho"] not in {l["caminho"] for l in linhas}]
    con.close()
    return {"resultados": [{"caminho": l["caminho"], "titulo": l["titulo"], "ementa": l["ementa"],
                            "tipo": l["tipo"], "ano": l["ano"], "esfera": l["esfera"], "uf": l["uf"] or None,
                            "revogada": bool(l["revogada"]), "fonte_oficial": l["fonte"],
                            "tamanho_caracteres": l["tamanho"]} for l in linhas]}


_ART = r"(?m)^[\s>*_#]*(?:\*\*)?Art\.?\s*(?:\*\*)?\s*"


@mcp.tool()
def ler_norma(caminho: str, artigo: str = "", ate_artigo: str = "", inicio: int = 0,
              max_caracteres: int = 15000) -> dict:
    """Lê o texto de uma norma pelo caminho devolvido nas buscas.

    artigo: lê só esse artigo, com parágrafos e incisos (ex.: "124", "124-A", "5").
    ate_artigo: com artigo, lê um intervalo (ex.: artigo="124", ate_artigo="136").
    Sem artigo: lê a partir de 'inicio' (em caracteres), até max_caracteres (máx. 60000).
    Leis grandes passam de 500 mil caracteres: prefira ler por artigo.
    """
    partes = caminho.replace("\\", "/").split("/")
    if caminho.startswith("/") or ".." in partes or not indice.caminho_indexavel(caminho):
        return {"erro": "caminho fora do acervo"}
    alvo = indice.REPOS_DIR / caminho
    if not alvo.is_file():
        return {"erro": "arquivo não encontrado"}
    texto = alvo.read_text(encoding="utf-8", errors="replace")
    max_caracteres = max(500, min(int(max_caracteres), 60000))
    if artigo:
        def achar(a):
            num = re.escape(a.strip().upper().replace("ART.", "").strip())
            return re.search(_ART + num + r"(?:\s*[º°o])?(?![\d]|-[A-Z])", texto)
        m = achar(artigo)
        if not m:
            return {"erro": f"artigo {artigo} não encontrado", "tamanho_caracteres": len(texto)}
        if ate_artigo:
            m2 = achar(ate_artigo)
            fim_base = m2.end() if m2 else m.end()
        else:
            fim_base = m.end()
        prox = re.compile(_ART + r"\d").search(texto, fim_base)
        fim = prox.start() if prox else len(texto)
        trecho = texto[m.start():fim].strip()
        return {"caminho": caminho, "artigo": artigo, "texto": trecho[:max_caracteres],
                "cortado": len(trecho) > max_caracteres}
    parte = texto[inicio: inicio + max_caracteres]
    return {"caminho": caminho, "inicio": inicio, "fim": inicio + len(parte),
            "tamanho_caracteres": len(texto), "texto": parte,
            "continua": inicio + len(parte) < len(texto)}


@mcp.tool()
def status_acervo() -> dict:
    """Mostra quantas normas estão indexadas, por esfera, e quando o acervo foi atualizado."""
    con = indice.conectar(somente_leitura=True)
    try:
        por = {r[0]: r[1] for r in con.execute("SELECT esfera, count(*) FROM normas GROUP BY esfera")}
        em = indice.meta(con, "indexado_em")
    except Exception:
        por, em = {}, None
    con.close()
    return {"estado": atualizador.estado, "normas_por_esfera": por,
            "total": sum(por.values()), "indexado_em": em}


if __name__ == "__main__":
    con = indice.conectar()
    indice.criar_esquema(con)
    con.close()
    atualizador.iniciar()
    print(f"Conector de leis no ar: http://0.0.0.0:{mcp.settings.port}/<TOKEN>/mcp", flush=True)
    mcp.run(transport="streamable-http")
