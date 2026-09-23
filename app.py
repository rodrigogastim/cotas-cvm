"""
Cotas CVM — histórico e performance de fundos a partir do Informe Diário da CVM.
Fonte: https://dados.cvm.gov.br/dataset/fi-doc-inf_diario

Rodar:  streamlit run app.py

Arquivos salvos ao lado do app:
  fundos.json        -> lista de fundos (CNPJ, nome, estratégia)
  cotas_salvas.csv   -> últimas cotas baixadas (abre o app já com os números)
  cache_cvm/         -> arquivos mensais da CVM (evita baixar de novo)
"""
import io
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

PASTA = Path(__file__).parent
BASE_URL = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{ym}.zip"
CACHE_DIR = PASTA / "cache_cvm"
CACHE_DIR.mkdir(exist_ok=True)
ARQ_FUNDOS = PASTA / "fundos.json"
ARQ_COTAS = PASTA / "cotas_salvas.csv"

ESTRATEGIAS = ["LO", "LB", "LS", "EH"]
DESC_ESTRATEGIA = "LO = Long Only · LB = Long Biased · LS = Long Short · EH = Equity Hedge"


# ================================================================ utilidades
def so_digitos(cnpj) -> str:
    d = re.sub(r"\D", "", str(cnpj or ""))
    return d.zfill(14) if d else ""


def fmt_cnpj(c: str) -> str:
    c = so_digitos(c)
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if c else ""


# ================================================================ lista de fundos
def carregar_fundos() -> pd.DataFrame:
    if ARQ_FUNDOS.exists():
        dados = json.loads(ARQ_FUNDOS.read_text(encoding="utf-8"))
    else:
        dados = []
    df = pd.DataFrame(dados, columns=["CNPJ", "Nome", "Estratégia"])
    return df.fillna("").astype(str)


def salvar_fundos(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("").astype(str).copy()
    df["CNPJ"] = df["CNPJ"].map(fmt_cnpj)
    df = df[df["CNPJ"] != ""].drop_duplicates("CNPJ")
    ARQ_FUNDOS.write_text(json.dumps(df.to_dict("records"), ensure_ascii=False, indent=2), encoding="utf-8")
    return df.reset_index(drop=True)


# ================================================================ download CVM
def meses_necessarios(hoje: date) -> list[str]:
    """Do mês de 13 meses atrás até o mês atual (cobre 12M, YTD e mês)."""
    fim = pd.Timestamp(hoje).to_period("M")
    return [p.strftime("%Y%m") for p in pd.period_range(fim - 13, fim, freq="M")]


def baixar_mes(ym: str, forcar: bool) -> Path | None:
    destino = CACHE_DIR / f"inf_diario_fi_{ym}.zip"
    if destino.exists() and not forcar:
        return destino
    r = requests.get(BASE_URL.format(ym=ym), timeout=180)
    if r.status_code == 404:  # mês corrente pode ainda não ter sido publicado
        return destino if destino.exists() else None
    r.raise_for_status()
    destino.write_bytes(r.content)
    return destino


def ler_mes(caminho: Path, cnpjs: set[str]) -> pd.DataFrame:
    with zipfile.ZipFile(caminho) as z:
        nome = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        raw = z.read(nome)
    df = pd.read_csv(io.BytesIO(raw), sep=";", dtype=str, encoding="latin-1")
    # Layout novo (RCVM 175): CNPJ_FUNDO_CLASSE / ID_SUBCLASSE; antigo: CNPJ_FUNDO
    col_cnpj = "CNPJ_FUNDO_CLASSE" if "CNPJ_FUNDO_CLASSE" in df.columns else "CNPJ_FUNDO"
    df["CNPJ"] = df[col_cnpj].str.replace(r"\D", "", regex=True).str.zfill(14)
    df = df[df["CNPJ"].isin(cnpjs)]
    if "ID_SUBCLASSE" not in df.columns:
        df["ID_SUBCLASSE"] = ""
    return df[["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA"]].copy()


def atualizar_cotas(cnpjs: list[str]) -> pd.DataFrame:
    """Baixa/lê os arquivos da CVM e grava cotas_salvas.csv."""
    meses = meses_necessarios(date.today())
    partes = []
    barra = st.progress(0.0, text="Baixando dados da CVM…")
    for i, ym in enumerate(meses):
        barra.progress((i + 1) / len(meses), text=f"Lendo {ym[4:]}/{ym[:4]}…")
        # mês atual e anterior são sempre rebaixados (a CVM revisa dados recentes)
        arq = baixar_mes(ym, forcar=i >= len(meses) - 2)
        if arq:
            partes.append(ler_mes(arq, set(cnpjs)))
    barra.empty()
    df = pd.concat(partes) if partes else pd.DataFrame(columns=["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA"])
    df = df.fillna({"ID_SUBCLASSE": ""}).drop_duplicates(["CNPJ", "ID_SUBCLASSE", "DT_COMPTC"], keep="last")
    df.to_csv(ARQ_COTAS, index=False)
    return df


def ler_cotas_salvas() -> pd.DataFrame | None:
    if not ARQ_COTAS.exists():
        return None
    df = pd.read_csv(ARQ_COTAS, dtype=str).fillna({"ID_SUBCLASSE": ""})
    df["CNPJ"] = df["CNPJ"].str.zfill(14)
    df["DT_COMPTC"] = pd.to_datetime(df["DT_COMPTC"])
    df["VL_QUOTA"] = pd.to_numeric(df["VL_QUOTA"], errors="coerce")
    return df.sort_values(["CNPJ", "ID_SUBCLASSE", "DT_COMPTC"])


# ================================================================ cálculo
def cota_em(s: pd.Series, data: pd.Timestamp):
    """Última cota disponível em ou antes de `data`."""
    s = s[s.index <= data]
    return s.iloc[-1] if len(s) else None


def retorno_pct(fim, ini):
    if fim is None or ini is None or ini == 0:
        return None
    return (fim / ini - 1) * 100


def performance(serie: pd.Series) -> dict:
    serie = serie.dropna().sort_index()
    ult_data, ult = serie.index[-1], serie.iloc[-1]
    ant = serie.iloc[-2] if len(serie) > 1 else None
    fim_mes_ant = ult_data.to_period("M").start_time - pd.Timedelta(days=1)
    fim_mes_ant2 = fim_mes_ant.to_period("M").start_time - pd.Timedelta(days=1)
    fim_ano_ant = pd.Timestamp(ult_data.year - 1, 12, 31)
    doze_m = ult_data - pd.DateOffset(years=1)
    return {
        "Última cota": ult_data.date(),
        "Cota": ult,
        "Dia": retorno_pct(ult, ant),
        "Mês (MTD)": retorno_pct(ult, cota_em(serie, fim_mes_ant)),
        "Mês anterior": retorno_pct(cota_em(serie, fim_mes_ant), cota_em(serie, fim_mes_ant2)),
        "YTD": retorno_pct(ult, cota_em(serie, fim_ano_ant)),
        "12 meses": retorno_pct(ult, cota_em(serie, doze_m)),
    }


# ================================================================ interface
st.set_page_config(page_title="Cotas CVM", layout="wide")
st.title("Cotas de fundos — CVM")
st.caption("Fonte: Informe Diário CVM (dados.cvm.gov.br). Performance calculada pela variação da cota.")

# ---------- Meus fundos (editável, salvo automaticamente)
with st.expander("Meus fundos — adicionar, renomear, classificar ou remover", expanded=not ARQ_FUNDOS.exists()):
    st.caption(
        "Adicione uma linha para cada fundo. Para remover, selecione a linha e aperte Delete. "
        f"As alterações são salvas automaticamente. {DESC_ESTRATEGIA}"
    )
    fundos = carregar_fundos()
    editado = st.data_editor(
        fundos,
        key="editor_fundos",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "CNPJ": st.column_config.TextColumn("CNPJ", help="Com ou sem pontuação", required=True),
            "Nome": st.column_config.TextColumn("Nome", help="Como você quer ver o fundo"),
            "Estratégia": st.column_config.SelectboxColumn("Estratégia", options=ESTRATEGIAS, help=DESC_ESTRATEGIA),
        },
    )
    editado = editado.fillna("").astype(str).reset_index(drop=True)
    sem_cnpj = (editado["CNPJ"].map(so_digitos) == "").any()
    if sem_cnpj:
        st.caption(":orange[Preencha o CNPJ da nova linha para salvar.]")
    elif not editado.equals(fundos.reset_index(drop=True)):
        salvar_fundos(editado)
        st.session_state.pop("editor_fundos", None)
        st.rerun()

fundos = carregar_fundos()
cnpjs = [so_digitos(c) for c in fundos["CNPJ"] if so_digitos(c)]

if not cnpjs:
    st.info("Adicione seus fundos em **Meus fundos** acima para começar.")
    st.stop()

# ---------- Botão de atualização
col_btn, col_info = st.columns([1, 4])
with col_btn:
    clicou = st.button("Atualizar cotas", type="primary", width="stretch")
cotas = ler_cotas_salvas()

faltando = set(cnpjs) - set(cotas["CNPJ"]) if cotas is not None else set(cnpjs)
if clicou:
    try:
        atualizar_cotas(cnpjs)
    except requests.RequestException as e:
        st.error(f"Não consegui baixar os dados da CVM: {e}")
    cotas = ler_cotas_salvas()
    faltando = set(cnpjs) - set(cotas["CNPJ"]) if cotas is not None else set(cnpjs)

with col_info:
    if ARQ_COTAS.exists():
        quando = datetime.fromtimestamp(ARQ_COTAS.stat().st_mtime)
        st.caption(f"Última atualização: {quando:%d/%m/%Y %H:%M}")
    if faltando and cotas is not None and not clicou:
        st.caption("Sem dados para " + ", ".join(fmt_cnpj(c) for c in sorted(faltando))
                   + " — clique em **Atualizar cotas** (se continuar, confira o CNPJ).")

if cotas is None:
    st.info("Clique em **Atualizar cotas** para baixar os dados pela primeira vez (leva alguns minutos).")
    st.stop()

# ---------- Tabela de performance
info = {so_digitos(r["CNPJ"]): r for r in fundos.to_dict("records")}
linhas, series = [], {}
for cnpj in cnpjs:
    d = cotas[cotas["CNPJ"] == cnpj]
    if d.empty:
        continue
    f = info[cnpj]
    nome_base = f["Nome"].strip() or fmt_cnpj(cnpj)
    for sub, g in d.groupby("ID_SUBCLASSE"):
        nome = nome_base + (f" · {sub}" if sub else "")
        s = g.set_index("DT_COMPTC")["VL_QUOTA"]
        series[nome] = s
        linhas.append({"Fundo": nome, "Estratégia": f["Estratégia"], "CNPJ": fmt_cnpj(cnpj), **performance(s)})

if faltando and clicou:
    st.warning("Não encontrados no Informe Diário: " + ", ".join(fmt_cnpj(c) for c in faltando))

if not linhas:
    st.stop()

tabela = pd.DataFrame(linhas)
filtro = st.multiselect("Filtrar por estratégia", ESTRATEGIAS, placeholder="Todas")
if filtro:
    tabela = tabela[tabela["Estratégia"].isin(filtro)]
tabela = tabela.sort_values(["Estratégia", "Fundo"])

pct = st.column_config.NumberColumn(format="%.2f%%")
st.subheader("Performance")
st.dataframe(
    tabela,
    hide_index=True,
    width="stretch",
    column_config={
        "Última cota": st.column_config.DateColumn(format="DD/MM/YYYY"),
        "Cota": st.column_config.NumberColumn(format="%.6f"),
        "Dia": pct, "Mês (MTD)": pct, "Mês anterior": pct, "YTD": pct, "12 meses": pct,
    },
)

# ---------- Gráfico e download
st.subheader("Histórico de cotas (base 100)")
visiveis = {k: v for k, v in series.items() if k in set(tabela["Fundo"])}
base100 = pd.DataFrame({k: v / v.dropna().iloc[0] * 100 for k, v in visiveis.items()})
st.line_chart(base100)

hist = pd.DataFrame(visiveis)
hist.index.name = "Data"
st.download_button(
    "Baixar histórico (CSV)",
    hist.to_csv(sep=";", decimal=",").encode("utf-8-sig"),
    file_name=f"cotas_cvm_{date.today():%Y%m%d}.csv",
    mime="text/csv",
)
