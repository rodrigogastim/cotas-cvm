"""
Persistência dos fundos no GitHub (para o app rodar no Streamlit Cloud sem perder dados).

Como usar no app.py:

    from fundos_store import carregar_fundos, salvar_fundos, status_persistencia, caixa_backup

    fundos = carregar_fundos()          # lista de dicionários
    ...
    fundos.append({"cnpj": cnpj, "nome": nome, "estrategia": "LO"})
    salvar_fundos(fundos)               # grava no GitHub (ou no arquivo local)

    st.sidebar.caption(status_persistencia())   # opcional: mostra onde está salvando

Formato do arquivo (fundos.json no repositório):

    [
      {"cnpj": "00.000.000/0001-00", "nome": "Fundo X", "estrategia": "LO"},
      ...
    ]

Se os Secrets do GitHub não estiverem configurados, o módulo grava em arquivo local —
útil para rodar no Windows, mas no Streamlit Cloud os dados se perdem no reinício.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import requests
import streamlit as st

ARQUIVO_LOCAL = "fundos.json"
API = "https://api.github.com"
TIMEOUT = 20


# ----------------------------------------------------------------- configuração
def _cfg() -> dict[str, str] | None:
    """Lê os Secrets do Streamlit. Retorna None se não estiverem configurados."""
    try:
        g = st.secrets["github"]
        return {
            "token": g["token"],
            "repo": g["repo"],                      # ex.: "rodrigo/cotas_cvm"
            "caminho": g.get("path", ARQUIVO_LOCAL),
            "branch": g.get("branch", "main"),
        }
    except Exception:
        return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def status_persistencia() -> str:
    c = _cfg()
    if c:
        return f"Salvando em {c['repo']} · {c['caminho']} (branch {c['branch']})"
    return "Salvando em arquivo local — no Streamlit Cloud os dados se perdem ao reiniciar"


# ----------------------------------------------------------------------- leitura
def _ler_github(c: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    """Retorna (fundos, sha). sha=None quando o arquivo ainda não existe."""
    r = requests.get(
        f"{API}/repos/{c['repo']}/contents/{c['caminho']}",
        headers=_headers(c["token"]),
        params={"ref": c["branch"]},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    dados = r.json()
    conteudo = base64.b64decode(dados["content"]).decode("utf-8")
    return json.loads(conteudo or "[]"), dados["sha"]


def carregar_fundos() -> list[dict[str, Any]]:
    c = _cfg()
    if not c:
        if os.path.exists(ARQUIVO_LOCAL):
            with open(ARQUIVO_LOCAL, encoding="utf-8") as f:
                return json.load(f)
        return []

    # o sha fica guardado na sessão para o próximo commit
    try:
        fundos, sha = _ler_github(c)
    except Exception as e:
        st.error(f"Não consegui ler a lista de fundos no GitHub: {e}")
        return st.session_state.get("_fundos_cache", [])

    st.session_state["_fundos_sha"] = sha
    st.session_state["_fundos_cache"] = fundos
    return fundos


# ------------------------------------------------------------------------ escrita
def salvar_fundos(fundos: list[dict[str, Any]], mensagem: str | None = None) -> bool:
    conteudo = json.dumps(fundos, ensure_ascii=False, indent=2) + "\n"
    c = _cfg()

    if not c:
        with open(ARQUIVO_LOCAL, "w", encoding="utf-8") as f:
            f.write(conteudo)
        return True

    corpo = {
        "message": mensagem or f"Atualiza lista de fundos ({len(fundos)} fundos)",
        "content": base64.b64encode(conteudo.encode("utf-8")).decode("ascii"),
        "branch": c["branch"],
    }
    sha = st.session_state.get("_fundos_sha")
    if sha:
        corpo["sha"] = sha

    url = f"{API}/repos/{c['repo']}/contents/{c['caminho']}"
    r = requests.put(url, headers=_headers(c["token"]), json=corpo, timeout=TIMEOUT)

    # 409/422: o sha guardado está velho (outra aba gravou antes) — relê e tenta de novo
    if r.status_code in (409, 422):
        try:
            _, sha_novo = _ler_github(c)
            if sha_novo:
                corpo["sha"] = sha_novo
            else:
                corpo.pop("sha", None)
            r = requests.put(url, headers=_headers(c["token"]), json=corpo, timeout=TIMEOUT)
        except Exception:
            pass

    if not r.ok:
        st.error(f"Não consegui salvar no GitHub ({r.status_code}): {r.text[:200]}")
        return False

    st.session_state["_fundos_sha"] = r.json()["content"]["sha"]
    st.session_state["_fundos_cache"] = fundos
    return True


# ------------------------------------------------- botões de backup (opcionais)
def caixa_backup(fundos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mostra download e upload da lista. Retorna a lista (nova, se houver import)."""
    st.download_button(
        "Baixar lista de fundos (JSON)",
        data=json.dumps(fundos, ensure_ascii=False, indent=2),
        file_name="fundos.json",
        mime="application/json",
    )
    arquivo = st.file_uploader("Restaurar lista a partir de um JSON", type="json")
    if arquivo is not None:
        try:
            novos = json.load(arquivo)
            if isinstance(novos, list) and salvar_fundos(novos, "Restaura lista de fundos"):
                st.success(f"{len(novos)} fundos restaurados.")
                return novos
            st.error("Arquivo inválido: esperava uma lista de fundos.")
        except Exception as e:
            st.error(f"Não consegui ler o arquivo: {e}")
    return fundos
