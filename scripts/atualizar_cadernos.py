"""
Robô dos Cadernos Técnicos SINAPI.

1. Abre a página de downloads da Caixa (sessão de navegador — sem ela a Caixa entra em loop de 302)
   e lista os PDFs SINAPI-CT-*.pdf.
2. Para cada PDF lê tamanho e data (Range 0-0). Compara com dados/estado.json.
3. Baixa a lista de composições da base SINAPI mais recente (robô miltonjose-sinapi) e associa cada
   grupo de composições ao caderno de nome mais parecido (mesma regra do extrator original).
4. Reprocessa só o que precisa: PDF novo/alterado, ou grupo cujas composições mudaram na base nova.
   Extração: localiza a página de cada composição e recorta as seções 3 a 9 do caderno.
5. Grava dados/cadernos-json/{caderno}.json + INDEX.json (formato já usado pelo Quantify) e
   saida/novidades.md (vira aviso por e-mail).

Variáveis: REPROCESSAR=1 força tudo · MAX_PDFS limita downloads por rodada (padrão: sem limite).
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"
JSONS = DADOS / "cadernos-json"
SAIDA = RAIZ / "saida"
for d in (JSONS, SAIDA):
    d.mkdir(parents=True, exist_ok=True)
ESTADO = DADOS / "estado.json"

PAGE_URL = "https://www.caixa.gov.br/site/Paginas/downloads.aspx#categoria_533"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
SINAPI_RAW = "https://raw.githubusercontent.com/engmiltonjose/miltonjose-sinapi/main/dados/"
REPROCESSAR = os.environ.get("REPROCESSAR", "").lower() in ("1", "true", "sim")
MAX_PDFS = int(os.environ.get("MAX_PDFS", "0") or 0)


def agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def slug(nome):
    """Mesmo nome de arquivo do extrator original (o Quantify procura por ele)."""
    return nome.lower().replace(" ", "-") + ".json"


# ─── Extração (lógica do processar-cadernos.py original) ─────────────────────

def extrair_secoes(bloco):
    secoes = {}
    for s in range(3, 10):
        m = re.search(rf"(?:^|\n)\s*{s}[\.\)\-\s]", bloco, re.M | re.I)
        if m:
            ini = m.start()
            if s < 9:
                m2 = re.search(rf"(?:^|\n)\s*{s + 1}[\.\)\-\s]", bloco[ini:], re.M | re.I)
                fim = ini + (m2.start() if m2 else len(bloco) - ini)
            else:
                fim = len(bloco)
            texto = bloco[ini:fim].strip()
            if texto:
                secoes[str(s)] = texto
    return secoes


def paginas_pdf(caminho):
    with pdfplumber.open(caminho) as p:
        return [(i + 1, pg.extract_text() or "") for i, pg in enumerate(p.pages)]


def localizar(paginas, codigos):
    codigo_set = set(codigos)
    inicio = []
    for pnum, txt in paginas:
        m_h = re.search(r"[Cc].{1,5}digo\s+Descri", txt)
        if not m_h:
            continue
        m_end = re.search(r"(?:Macroclasse|1\s*[\.\)])", txt[m_h.end():])
        zona = txt[m_h.end(): m_h.end() + m_end.start()] if m_end else txt[m_h.end():]
        for cod in codigo_set:
            if re.search(rf"\b{re.escape(cod)}\b", zona):
                inicio.append((pnum, cod))
                break
    inicio.sort()
    out = {}
    for i, (pini, cod) in enumerate(inicio):
        pfim = inicio[i + 1][0] if i + 1 < len(inicio) else len(paginas) + 1
        bloco = "\n".join(t for n, t in paginas if pini <= n < pfim)
        out[cod] = (pini, extrair_secoes(bloco))
    return out


# ─── Caixa ───────────────────────────────────────────────────────────────────

def listar_pdfs(page):
    page.goto(PAGE_URL, wait_until="networkidle", timeout=120000)
    time.sleep(4)
    for sel in ("[data-categoria='533']", "a[href='#categoria_533']"):
        try:
            page.click(sel, timeout=5000)
            time.sleep(3)
            break
        except Exception:
            pass
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
    pdfs = {}
    for h in hrefs:
        m = re.search(r"/sinapi-cadernos-tecnicos/(SINAPI-CT-[^/?#]+\.pdf)", h, re.I)
        if m:
            pdfs[m.group(1)] = h
    return pdfs


def versao_pdf(ctx, url):
    r = ctx.request.get(url, headers={"Referer": PAGE_URL, "Range": "bytes=0-0"}, timeout=120000)
    if r.status not in (200, 206):
        raise RuntimeError(f"HTTP {r.status}")
    total = (r.headers.get("content-range") or "").split("/")[-1] or r.headers.get("content-length")
    return {"tamanho": int(total) if total and total.isdigit() else None,
            "modificado": r.headers.get("last-modified") or "", "etag": r.headers.get("etag") or ""}


def baixar_pdf(ctx, url, destino):
    r = ctx.request.get(url, headers={"Referer": PAGE_URL}, timeout=300000)
    corpo = r.body()
    if r.status != 200 or not corpo.startswith(b"%PDF"):
        raise RuntimeError(f"download inválido (HTTP {r.status})")
    destino.write_bytes(corpo)
    return hashlib.sha1(corpo).hexdigest()


# ─── Principal ───────────────────────────────────────────────────────────────

def main():
    from playwright.sync_api import sync_playwright

    estado = json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {"pdfs": {}, "grupos": {}}
    man = get_json(SINAPI_RAW + "manifesto.json")
    base = man["mais_recente"]
    comps = get_json(SINAPI_RAW + f"sinapi-composicoes-{base}.json")
    grupos = {}
    for c in comps:
        grupos.setdefault(c.get("grupo", ""), []).append(c["codigo"])
    print(f"Base SINAPI {base}: {len(comps)} composições em {len(grupos)} grupos")

    novidades, erros, processados = [], [], 0
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, locale="pt-BR")
        page = ctx.new_page()
        pdfs = listar_pdfs(page)
        print(f"PDFs de cadernos na Caixa: {len(pdfs)}")
        if not pdfs:
            raise SystemExit("Nenhum caderno encontrado — a página da Caixa pode ter mudado.")

        # grupo → PDF de nome mais parecido (regra do extrator original)
        nomes = list(pdfs)
        grupo_pdf = {}
        for g in grupos:
            alvo = g.upper()
            melhor = max(nomes, key=lambda n: SequenceMatcher(
                None, alvo, n.replace("SINAPI-CT-", "").replace(".pdf", "").replace("-", " ").upper()).ratio())
            grupo_pdf[g] = melhor

        # O que precisa reprocessar
        versoes, mudou_pdf = {}, set()
        for nome, url in pdfs.items():
            try:
                v = versao_pdf(ctx, url)
            except Exception as e:
                erros.append(f"{nome}: não consegui ler a versão ({e})")
                continue
            versoes[nome] = v
            ant = estado["pdfs"].get(nome, {})
            if REPROCESSAR or (v["tamanho"], v["modificado"]) != (ant.get("tamanho"), ant.get("modificado")):
                mudou_pdf.add(nome)
        a_fazer = {}
        for g, cods in grupos.items():
            pdf = grupo_pdf[g]
            assinatura = hashlib.sha1(",".join(sorted(cods)).encode()).hexdigest()
            arq_existe = (JSONS / slug(g)).exists()
            if pdf in mudou_pdf or estado["grupos"].get(g, {}).get("assinatura") != assinatura or not arq_existe:
                a_fazer.setdefault(pdf, []).append((g, assinatura))
        alvo_pdfs = sorted(a_fazer)
        if MAX_PDFS:
            alvo_pdfs = alvo_pdfs[:MAX_PDFS]
        print(f"PDFs a (re)processar nesta rodada: {len(alvo_pdfs)}")

        for nome in alvo_pdfs:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    arq = Path(tmp) / nome
                    sha = baixar_pdf(ctx, pdfs[nome], arq)
                    paginas = paginas_pdf(arq)
                for g, assinatura in a_fazer[nome]:
                    extr = localizar(paginas, grupos[g])
                    dados = {}
                    for cod in grupos[g]:
                        pag, sec = extr.get(cod, (None, {}))
                        dados[cod] = {"caderno": g, "arquivo": nome, "pagina_inicial": pag, "secoes": sec,
                                      "pdf_url": pdfs[nome], "pdf_modificado": versoes[nome]["modificado"],
                                      "base_sinapi": base, "processado_em": agora()}
                    (JSONS / slug(g)).write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
                    achadas = sum(1 for c in dados.values() if c["pagina_inicial"])
                    estado["grupos"][g] = {"pdf": nome, "assinatura": assinatura, "composicoes": len(dados),
                                           "com_texto": achadas, "processado_em": agora()}
                    novidades.append((g, nome, len(dados), achadas, nome in mudou_pdf))
                estado["pdfs"][nome] = {**versoes[nome], "sha1": sha, "url": pdfs[nome], "processado_em": agora()}
                processados += 1
                print(f"  ok {nome}: {len(a_fazer[nome])} grupo(s)")
            except Exception as e:
                erros.append(f"{nome}: {e}")
                print(f"  ERRO {nome}: {e}")
        b.close()

    # INDEX no formato do Quantify
    idx_data, idx_cad, total = {}, {}, 0
    for f in sorted(JSONS.glob("*.json")):
        if f.name == "INDEX.json":
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        if not d:
            continue
        g = next(iter(d.values()))["caderno"]
        idx_cad[g] = {"arquivo": f.name, "composicoes": list(d)}
        for cod in d:
            idx_data[cod] = g
        total += len(d)
    (JSONS / "INDEX.json").write_text(json.dumps(
        {"total_composicoes": total, "total_cadernos": len(idx_cad), "base_sinapi": base, "atualizado_em": agora(),
         "data": idx_data, "cadernos": idx_cad}, ensure_ascii=False, indent=2), encoding="utf-8")
    estado["base_sinapi"] = base
    estado["atualizado_em"] = agora()
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")

    if novidades or erros:
        L = []
        if novidades:
            L += [f"## ✅ Cadernos técnicos atualizados (base SINAPI {base})", "",
                  "| Caderno (grupo) | PDF da Caixa | Composições | Com texto extraído | Motivo |", "|---|---|---|---|---|"]
            for g, nome, n, ach, pdf_mudou in novidades:
                L.append(f"| {g} | `{nome}` | {n} | {ach} | {'PDF novo/alterado' if pdf_mudou else 'composições mudaram'} |")
            L += ["", "Para as ferramentas: https://raw.githubusercontent.com/engmiltonjose/miltonjose-cadernostecnicos/main/dados/cadernos-json/INDEX.json"]
        if erros:
            L += ["", "## ❌ O que não deu certo", ""] + [f"- {e}" for e in erros[:40]] + [
                "", "**O que fazer:** nada por enquanto — o robô tenta de novo na próxima rodada. "
                "Se repetir, peça ao Claude para olhar o log."]
        (SAIDA / "novidades.md").write_text("\n".join(L), encoding="utf-8")
        (SAIDA / "titulo.txt").write_text(
            f"✅ Cadernos técnicos SINAPI: {len({n for _, n, *_ in novidades})} PDF(s) processado(s)" if novidades
            else "❌ Robô dos cadernos técnicos com falha", encoding="utf-8")
    print(f"PDFs processados: {processados} | erros: {len(erros)}")


if __name__ == "__main__":
    main()
