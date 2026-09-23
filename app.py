"""
Cotas CVM — histórico e performance de fundos a partir do Informe Diário da CVM.
Fonte: https://dados.cvm.gov.br/dataset/fi-doc-inf_diario

Rodar:  streamlit run app.py
"""
import io
import re
import time
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{ym}.zip"
CACHE_DIR = Path(__file__).parent / "cache_cvm"
CACHE_DIR.mkdir(exist_ok=True)
REFRESH_HOURS = 6  # mês corrente é rebaixado se o arquivo local tiver mais que isso


# ---------------------------------------------------------------- dados
def so_digitos(cnpj: str) -> str:
    return re.sub(r"\D", "", str(cnpj)).zfill(14)


def meses_necessarios(hoje: date) -> list[str]:
    """Do mês de 13 meses atrás até o mês atual (cobre 12M, YTD e mês)."""
    ini = pd.Timestamp(hoje).to_period("M") - 13
    fim = pd.Timestamp(hoje).to_period("M")
    return [p.strftime("%Y%m") for p in pd.period_range(ini, fim, freq="M")]


def baixar_mes(ym: str, mes_corrente: bool) -> Path | None:
    destino = CACHE_DIR / f"inf_diario_fi_{ym}.zip"
    if destino.exists():
        idade_h = (time.time() - destino.stat().st_mtime) / 3600
        if not mes_corrente or idade_h < REFRESH_HOURS:
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
    out = df[["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA", "VL_PATRIM_LIQ"]].copy()
    out["ID_SUBCLASSE"] = out["ID_SUBCLASSE"].fillna("")
    out["DT_COMPTC"] = pd.to_datetime(out["DT_COMPTC"])
    out["VL_QUOTA"] = pd.to_numeric(out["VL_QUOTA"], errors="coerce")
    out["VL_PATRIM_LIQ"] = pd.to_numeric(out["VL_PATRIM_LIQ"], errors="coerce")
    return out


@st.cache_data(ttl=REFRESH_HOURS * 3600, show_spinner=False)
def carregar(cnpjs: tuple[str, ...], hoje: date) -> pd.DataFrame:
    meses = meses_necessarios(hoje)
    partes, barra = [], st.progress(0.0, text="Baixando dados da CVM…")
    for i, ym in enumerate(meses):
        barra.progress((i + 1) / len(meses), text=f"Lendo {ym[4:]}/{ym[:4]}…")
        arq = baixar_mes(ym, mes_corrente=(i == len(meses) - 1))
        if arq:
            partes.append(ler_mes(arq, set(cnpjs)))
    barra.empty()
    if not partes:
        return pd.DataFrame()
    df = pd.concat(partes).drop_duplicates(["CNPJ", "ID_SUBCLASSE", "DT_COMPTC"])
    return df.sort_values(["CNPJ", "ID_SUBCLASSE", "DT_COMPTC"])


# ---------------------------------------------------------------- cálculo
def cota_em(s: pd.Series, data: pd.Timestamp) -> float | None:
    """Última cota disponível em ou antes de `data`."""
    s = s[s.index <= data]
    return s.iloc[-1] if len(s) else None


def retorno(fim, ini):
    return (fim / ini - 1) if (fim is not None and ini not in (None, 0)) else None


def performance(serie: pd.Series) -> dict:
    serie = serie.dropna().sort_index()
    ult_data, ult = serie.index[-1], serie.iloc[-1]
    ant = serie.iloc[-2] if len(serie) > 1 else None
    fim_mes_ant = ult_data.to_period("M").start_time - pd.Timedelta(days=1)
    fim_mes_ant2 = fim_mes_ant.to_period("M").start_time - pd.Timedelta(days=1)
    fim_ano_ant = pd.Timestamp(ult_data.year - 1, 12, 31)
    doze_m = ult_data - pd.DateOffset(years=1)
    return {
        "Data última cota": ult_data.date(),
        "Cota": ult,
        "Dia": retorno(ult, ant),
        "Mês atual (MTD)": retorno(ult, cota_em(serie, fim_mes_ant)),
        f"Mês anterior ({fim_mes_ant:%m/%y})": retorno(cota_em(serie, fim_mes_ant), cota_em(serie, fim_mes_ant2)),
        "YTD": retorno(ult, cota_em(serie, fim_ano_ant)),
        "12 meses": retorno(ult, cota_em(serie, doze_m)),
    }


# ---------------------------------------------------------------- interface
st.set_page_config(page_title="Cotas CVM", layout="wide")
st.title("Cotas de fundos — CVM")
st.caption("Fonte: Informe Diário CVM (dados.cvm.gov.br). Performance calculada pela variação da cota.")

entrada = st.text_area(
    "CNPJs (um por linha, com ou sem pontuação)",
    placeholder="00.000.000/0000-00",
    height=120,
)
cnpjs = tuple(sorted({so_digitos(c) for c in re.split(r"[\n,;]+", entrada) if re.sub(r"\D", "", c)}))

if st.button("Buscar", type="primary", disabled=not cnpjs):
    st.session_state["cnpjs"] = cnpjs

if "cnpjs" in st.session_state:
    cnpjs = st.session_state["cnpjs"]
    try:
        dados = carregar(cnpjs, date.today())
    except requests.RequestException as e:
        st.error(f"Não consegui baixar os dados da CVM: {e}")
        st.stop()

    linhas, series = [], {}
    for cnpj in cnpjs:
        d = dados[dados["CNPJ"] == cnpj] if len(dados) else dados
        if d.empty:
            st.warning(f"CNPJ {cnpj} não encontrado no Informe Diário.")
            continue
        for sub, g in d.groupby("ID_SUBCLASSE"):
            rotulo = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}" + (f" · {sub}" if sub else "")
            s = g.set_index("DT_COMPTC")["VL_QUOTA"]
            series[rotulo] = s
            linhas.append({"Fundo": rotulo, **performance(s)})

    if linhas:
        tabela = pd.DataFrame(linhas).set_index("Fundo")
        pct_cols = [c for c in tabela.columns if c not in ("Data última cota", "Cota")]
        st.subheader("Performance")
        st.dataframe(
            tabela.style.format({c: "{:.2%}" for c in pct_cols}, na_rep="–").format({"Cota": "{:.6f}"}),
            width="stretch",
        )

        st.subheader("Histórico de cotas (base 100)")
        base100 = pd.DataFrame({k: v / v.dropna().iloc[0] * 100 for k, v in series.items()})
        st.line_chart(base100)

        hist = pd.DataFrame(series)
        hist.index.name = "Data"
        st.download_button(
            "Baixar histórico (CSV)",
            hist.to_csv(sep=";", decimal=",").encode("utf-8-sig"),
            file_name=f"cotas_cvm_{date.today():%Y%m%d}.csv",
            mime="text/csv",
        )
