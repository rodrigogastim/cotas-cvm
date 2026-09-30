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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import requests
import streamlit as st

PASTA = Path(__file__).parent
BASE_URL = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{ym}.zip"
CACHE_DIR = PASTA / "cache_cvm"
CACHE_DIR.mkdir(exist_ok=True)
ARQ_FUNDOS = PASTA / "fundos.json"
ARQ_COTAS = PASTA / "cotas_salvas.csv"

ESTRATEGIAS = ["LB", "LO", "LS", "EH"]  # também é a ordem de exibição
DESC_ESTRATEGIA = "LB = Long Biased · LO = Long Only · LS = Long Short · EH = Equity Hedge"
NOME_ESTRATEGIA = {"LB": "Long Biased", "LO": "Long Only", "LS": "Long Short", "EH": "Equity Hedge"}
ARQ_BENCH = PASTA / "benchmarks_v2.csv"  # v2: IMA-B oficial da ANBIMA
BENCH_POR_TIPO = {"LB": ["IPCA + IMA-B"], "LO": ["Ibovespa"], "LS": ["CDI"], "EH": ["CDI"]}
DESC_BENCH = {
    "CDI": "CDI (Banco Central)",
    "IPCA": "IPCA (IBGE via Banco Central; mês ainda não divulgado fica estável)",
    "IMA-B": "IMA-B (ANBIMA)",
    "IPCA + IMA-B": "IPCA + IMA-B (soma dos retornos diários; IMA-B da ANBIMA, IPCA do mês ainda não divulgado = 0)",
    "Ibovespa": "Ibovespa",
}
UA = {"User-Agent": "Mozilla/5.0"}


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
    df = pd.DataFrame(dados, columns=["CNPJ", "Nome", "Estratégia", "Subclasse"])
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
    return df[["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA", "VL_PATRIM_LIQ"]].copy()


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
    if "VL_PATRIM_LIQ" in df:
        df["VL_PATRIM_LIQ"] = pd.to_numeric(df["VL_PATRIM_LIQ"], errors="coerce")
    return df.sort_values(["CNPJ", "ID_SUBCLASSE", "DT_COMPTC"])


# ================================================================ benchmarks
def _bcb(codigo: int, ini: date, fim: date) -> pd.Series:
    url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
           f"?formato=json&dataInicial={ini:%d/%m/%Y}&dataFinal={fim:%d/%m/%Y}")
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    return pd.Series(pd.to_numeric(df["valor"]).values, index=pd.to_datetime(df["data"], dayfirst=True))


def _indice_de_taxas(taxas_dia: pd.Series) -> pd.Series:
    """Taxas diárias (%) -> índice; o valor da data d acumula as taxas dos dias anteriores a d."""
    fator = 1 + taxas_dia.sort_index() / 100
    return fator.cumprod().shift(1).fillna(1.0)


def bench_cdi(ini: date, fim: date) -> pd.Series:
    return _indice_de_taxas(_bcb(12, ini, fim))


def bench_ipca(ini: date, fim: date) -> pd.Series:
    """IPCA mensal distribuído nos dias úteis de cada mês (juros compostos)."""
    mensal = _bcb(433, ini.replace(day=1), fim)
    dias = pd.bdate_range(pd.Timestamp(ini).to_period("M").start_time, fim)
    taxa_mes = {k.to_period("M"): v for k, v in mensal.items()}
    n_dias = pd.Series(1, index=dias).groupby(dias.to_period("M")).transform("sum")
    taxas = [((1 + taxa_mes.get(d.to_period("M"), 0) / 100) ** (1 / n_dias[d]) - 1) * 100 for d in dias]
    return _indice_de_taxas(pd.Series(taxas, index=dias))


def bench_yahoo(ticker: str, ini: date) -> pd.Series:
    p1 = int(pd.Timestamp(ini).timestamp())
    p2 = int(pd.Timestamp.now().timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?period1={p1}&period2={p2}&interval=1d"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    datas = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/Sao_Paulo").normalize().tz_localize(None)
    ind = res["indicators"]
    precos = ind.get("adjclose", [{}])[0].get("adjclose") or ind["quote"][0]["close"]
    s = pd.Series(precos, index=datas, dtype="float64").dropna()
    return s[~s.index.duplicated(keep="last")]


def _imab_dia(dia: pd.Timestamp):
    """Número-índice do IMA-B numa data (planilha diária da ANBIMA). None se não houver."""
    corpo = {"Tipo": "", "DataRef": "", "Pai": "ima", "escolha": "2", "Idioma": "PT", "saida": "csv",
             "Dt_Ref_Ver": f"{dia:%Y%m%d}", "Dt_Ref": f"{dia:%d/%m/%Y}"}
    for _ in range(3):
        try:
            r = requests.post("https://www.anbima.com.br/informacoes/ima/ima-sh-down.asp",
                              data=corpo, headers=UA, timeout=30)
            for linha in r.content.decode("latin-1").splitlines():
                if linha.startswith("IMA-B;"):
                    c = linha.split(";")
                    return pd.to_datetime(c[1], dayfirst=True), float(c[2].replace(".", "").replace(",", "."))
            return None
        except requests.RequestException:
            continue
    return None


def bench_imab(ini: date, fim: date, anterior: pd.Series | None) -> pd.Series:
    """IMA-B oficial da ANBIMA; baixa só os dias que ainda não estão salvos (e refaz os 3 últimos)."""
    ja_tem = anterior.dropna() if anterior is not None else pd.Series(dtype="float64")
    dias = pd.bdate_range(ini, fim)
    faltam = [d for d in dias if d not in ja_tem.index] + list(dias[-3:])
    with ThreadPoolExecutor(max_workers=6) as ex:
        novos = [x for x in ex.map(_imab_dia, sorted(set(faltam))) if x]
    if not novos and ja_tem.empty:
        raise RuntimeError("ANBIMA sem resposta")
    s = pd.concat([ja_tem, pd.Series({d: v for d, v in novos}, dtype="float64")])
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s.index >= pd.Timestamp(ini)]


def atualizar_benchmarks() -> list[str]:
    """Baixa os benchmarks e grava benchmarks.csv. Retorna a lista de falhas."""
    fim = date.today()
    ini = (pd.Timestamp(fim) - pd.DateOffset(months=14)).date()
    antigos = ler_benchmarks()
    fontes = {
        "CDI": lambda: bench_cdi(ini, fim),
        "IPCA": lambda: bench_ipca(ini, fim),
        "IMA-B": lambda: bench_imab(ini, fim, antigos["IMA-B"] if antigos is not None and "IMA-B" in antigos else None),
        "Ibovespa": lambda: bench_yahoo("^BVSP", ini),
    }
    series, falhas = {}, []
    for nome, f in fontes.items():
        try:
            series[nome] = f()
        except Exception:  # noqa: BLE001 — um benchmark fora do ar não pode derrubar o app
            falhas.append(nome)
            if antigos is not None and nome in antigos:
                series[nome] = antigos[nome].dropna()
    if series:
        df = pd.DataFrame(series)
        df.index.name = "Data"
        df.to_csv(ARQ_BENCH)
    return falhas


def ler_benchmarks() -> pd.DataFrame | None:
    if not ARQ_BENCH.exists():
        return None
    df = pd.read_csv(ARQ_BENCH, index_col="Data", parse_dates=["Data"])
    if {"IPCA", "IMA-B"} <= set(df.columns):
        df["IPCA + IMA-B"] = benchmark_soma(df["IPCA"], df["IMA-B"])
    return df


def benchmark_soma(ipca: pd.Series, imab: pd.Series) -> pd.Series:
    """Índice cujo retorno diário = retorno diário do IPCA + retorno diário do IMA-B."""
    imab = imab.dropna()
    ipca = ipca.dropna().reindex(ipca.dropna().index.union(imab.index)).ffill().reindex(imab.index)
    r = ipca.pct_change().fillna(0) + imab.pct_change().fillna(0)
    return (1 + r).cumprod()


# ================================================================ cálculo
PERIODOS = ["Dia", "Mês (MTD)", "Mês anterior", "YTD", "12 meses"]
PERIODOS_CDI = ["YTD", "12 meses"]  # comparação com o CDI só nesses períodos


def cota_em(s: pd.Series, data):
    """Última cota disponível em ou antes de `data`."""
    if data is None:
        return None
    s = s[s.index <= data]
    return s.iloc[-1] if len(s) else None


def janelas(serie: pd.Series) -> dict:
    """Datas (início, fim) de cada período, a partir da última cota da série."""
    ult = serie.index[-1]
    ant = serie.index[-2] if len(serie) > 1 else None
    fm1 = ult.to_period("M").start_time - pd.Timedelta(days=1)
    fm2 = fm1.to_period("M").start_time - pd.Timedelta(days=1)
    return {
        "Dia": (ant, ult),
        "Mês (MTD)": (fm1, ult),
        "Mês anterior": (fm2, fm1),
        "YTD": (pd.Timestamp(ult.year - 1, 12, 31), ult),
        "12 meses": (ult - pd.DateOffset(years=1), ult),
    }


def retorno_pct(s: pd.Series, ini, fim):
    a, b = cota_em(s, ini), cota_em(s, fim)
    if a is None or b is None or a == 0:
        return None
    return (b / a - 1) * 100


def linha_performance(serie: pd.Series, cdi: pd.Series | None, com_cdi: bool) -> dict:
    serie = serie.dropna().sort_index()
    jan = janelas(serie)
    out = {"Última cota": serie.index[-1].date(), "Cota": serie.iloc[-1]}
    for p, (a, b) in jan.items():
        out[p] = retorno_pct(serie, a, b)
    if com_cdi:
        for p, (a, b) in jan.items():
            r_cdi = retorno_pct(cdi, a, b) if cdi is not None else None
            r = out[p]
            out[f"{p} %CDI"] = (r / r_cdi * 100) if (r is not None and r_cdi and r_cdi > 0) else None
    return out


# ================================================================ gráfico
# Esquema de cores Itaú BBA (hub de branding e design).
AZUL_IBBA, LARANJA_IBBA, AMARELO = "#000512", "#FF5500", "#F7D317"
VERDE_POS, VERMELHO_NEG = "#1E7B53", "#B3261E"  # texto de retorno positivo / negativo
CINZA = {85: "#272B35", 70: "#4E5159", 55: "#74767D", 40: "#9A9BA0", 25: "#C0C1C4", 10: "#E6E6E7"}
# Fundos: laranja primeiro, depois auxiliares da marca em ordem fixa (validada p/ daltonismo).
PALETA = [LARANJA_IBBA, "#2192A5", CINZA[70], "#1EC86D", CINZA[40], "#FD8309", "#57B49A", CINZA[25]]
# Benchmark em azul IBBA (tracejado); um segundo benchmark, se houver, em cinza.
CORES_BENCH = [AZUL_IBBA, CINZA[55]]


def grafico(series_fundos: dict, series_bench: dict, inicio: pd.Timestamp) -> alt.Chart | None:
    linhas = []
    for tipo, grupo in (("Fundo", series_fundos), ("Benchmark", series_bench)):
        for nome, s in grupo.items():
            s = s.dropna().sort_index()
            base_data = s.index[s.index <= inicio]
            if len(base_data) == 0:
                continue
            s = s[s.index >= base_data[-1]]
            linhas.append(pd.DataFrame({"Data": s.index, "Série": nome, "Tipo": tipo,
                                        "Valor": (s / s.iloc[0] * 100).values}))
    if not linhas:
        return None
    df = pd.concat(linhas)
    nomes_f = sorted(series_fundos)  # cor segue o fundo (ordem alfabética), não o ranking
    nomes_b = list(series_bench)
    dominio = nomes_f + nomes_b
    cores = [PALETA[i % len(PALETA)] for i in range(len(nomes_f))] + CORES_BENCH[: len(nomes_b)]

    perto = alt.selection_point(nearest=True, on="pointerover", fields=["Data"], empty=False)
    base = alt.Chart(df).encode(
        x=alt.X("Data:T", title=None, axis=alt.Axis(format="%b/%y", grid=False, tickCount="month",
                                                      labelColor=CINZA[70], domainColor=CINZA[25])),
        y=alt.Y("Valor:Q", title="Base 100", scale=alt.Scale(zero=False),
                axis=alt.Axis(gridColor=CINZA[10], gridDash=[2, 2], labelColor=CINZA[70],
                              titleColor=CINZA[70], domain=False)),
        color=alt.Color("Série:N", scale=alt.Scale(domain=dominio, range=cores),
                        legend=alt.Legend(title=None, orient="bottom", columns=3, labelLimit=260,
                                          labelColor=AZUL_IBBA)),
    )
    linhas_ch = base.mark_line(strokeWidth=2).encode(
        strokeDash=alt.StrokeDash("Tipo:N", scale=alt.Scale(domain=["Fundo", "Benchmark"],
                                                           range=[[1, 0], [6, 4]]), legend=None)
    )
    alvo = base.mark_point(size=120).encode(opacity=alt.value(0)).add_params(perto)
    pontos = base.mark_point(filled=True, size=50).encode(
        opacity=alt.condition(perto, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("Série:N"), alt.Tooltip("Data:T", format="%d/%m/%Y"),
                 alt.Tooltip("Valor:Q", format=".2f", title="Base 100")],
    )
    regua = alt.Chart(df).mark_rule(color=CINZA[25]).encode(x="Data:T").transform_filter(perto)
    return (linhas_ch + alvo + pontos + regua).properties(height=340)


# ================================================================ histórico desde o início
# Meses anteriores à janela de cotas_salvas.csv são lidos dos arquivos da CVM (mensais desde
# 2021 e anuais em HIST/ até 2020) só quando o usuário abre o histórico de um fundo.
# O resultado fica salvo (mês a mês, primeira e última cota) para não baixar de novo.
HIST_URL = BASE_URL.replace("inf_diario_fi_{ym}.zip", "HIST/inf_diario_fi_{ano}.zip")
ARQ_HIST = PASTA / "historico_mensal.csv"
ARQ_HIST_STATUS = PASTA / "historico_status.json"
MESES_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
COLS_HIST = ["CNPJ", "ID_SUBCLASSE", "MES", "DT_INI", "COTA_INI", "DT_FIM", "COTA_FIM"]


def _periodos_antes(mes_limite: str) -> list[tuple[str, str]]:
    """Períodos (do mais novo ao mais antigo) com meses anteriores a `mes_limite` (AAAA-MM)."""
    out = []
    p = pd.Period(mes_limite, "M") - 1
    while p >= pd.Period("2021-01", "M"):
        out.append(("M", p.strftime("%Y%m")))
        p -= 1
    for ano in range(min(2020, p.year), 1999, -1):
        out.append(("A", str(ano)))
    return out


def _ultimo_mes(per) -> str:
    tipo, v = per
    return f"{v[:4]}-{v[4:]}" if tipo == "M" else f"{v}-12"


def _mes_seguinte(mes: str) -> str:
    return (pd.Period(mes, "M") + 1).strftime("%Y-%m")


def _ler_status() -> dict:
    return json.loads(ARQ_HIST_STATUS.read_text()) if ARQ_HIST_STATUS.exists() else {}


def _ler_hist() -> pd.DataFrame:
    if not ARQ_HIST.exists():
        return pd.DataFrame(columns=COLS_HIST)
    df = pd.read_csv(ARQ_HIST, dtype={"CNPJ": str, "ID_SUBCLASSE": str, "MES": str}).fillna({"ID_SUBCLASSE": ""})
    df["CNPJ"] = df["CNPJ"].str.zfill(14)
    return df


def _agregar_mensal(df: pd.DataFrame) -> pd.DataFrame:
    """Cotas diárias -> uma linha por (CNPJ, subclasse, mês) com a primeira e a última cota."""
    if df.empty:
        return pd.DataFrame(columns=COLS_HIST)
    df = df.dropna(subset=["VL_QUOTA"]).sort_values("DT_COMPTC")
    df["MES"] = df["DT_COMPTC"].dt.strftime("%Y-%m")
    g = df.groupby(["CNPJ", "ID_SUBCLASSE", "MES"])
    out = pd.DataFrame({
        "DT_INI": g["DT_COMPTC"].first(), "COTA_INI": g["VL_QUOTA"].first(),
        "DT_FIM": g["DT_COMPTC"].last(), "COTA_FIM": g["VL_QUOTA"].last(),
    }).reset_index()
    out["DT_INI"] = out["DT_INI"].dt.strftime("%Y-%m-%d")
    out["DT_FIM"] = out["DT_FIM"].dt.strftime("%Y-%m-%d")
    return out[COLS_HIST]


def _extrair_arquivo(caminho: Path, cnpjs: set[str]) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Lê um zip da CVM em pedaços (os anuais são grandes) e devolve só as linhas dos fundos pedidos."""
    usar = {"CNPJ_FUNDO", "CNPJ_FUNDO_CLASSE", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA"}
    partes, dt_min = [], None
    with zipfile.ZipFile(caminho) as z:
        for nome in [n for n in z.namelist() if n.lower().endswith(".csv")]:
            with z.open(nome) as f:
                for ch in pd.read_csv(f, sep=";", dtype=str, encoding="latin-1",
                                      usecols=lambda c: c in usar, chunksize=400_000):
                    datas = pd.to_datetime(ch["DT_COMPTC"], errors="coerce")
                    m = datas.min()
                    dt_min = m if dt_min is None or (pd.notna(m) and m < dt_min) else dt_min
                    col = "CNPJ_FUNDO_CLASSE" if "CNPJ_FUNDO_CLASSE" in ch else "CNPJ_FUNDO"
                    ch["CNPJ"] = ch[col].str.replace(r"\D", "", regex=True).str.zfill(14)
                    ch = ch[ch["CNPJ"].isin(cnpjs)]
                    if ch.empty:
                        continue
                    if "ID_SUBCLASSE" not in ch:
                        ch["ID_SUBCLASSE"] = ""
                    ch = ch.assign(DT_COMPTC=pd.to_datetime(ch["DT_COMPTC"]),
                                   VL_QUOTA=pd.to_numeric(ch["VL_QUOTA"], errors="coerce"))
                    partes.append(ch[["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA"]].fillna({"ID_SUBCLASSE": ""}))
    df = pd.concat(partes) if partes else pd.DataFrame(columns=["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA"])
    return df, dt_min


def _arquivo_do_periodo(per) -> tuple[Path | None, bool]:
    """Devolve (caminho, apagar_depois). Usa o cache dos meses recentes quando existe."""
    tipo, v = per
    if tipo == "M":
        cache = CACHE_DIR / f"inf_diario_fi_{v}.zip"
        if cache.exists():
            return cache, False
        url = BASE_URL.format(ym=v)
    else:
        url = HIST_URL.format(ano=v)
    destino = CACHE_DIR / "tmp_historico.zip"
    with requests.get(url, stream=True, timeout=300) as r:
        if r.status_code == 404:
            return None, False
        r.raise_for_status()
        with open(destino, "wb") as f:
            for bloco in r.iter_content(1 << 20):
                f.write(bloco)
    return destino, True


def garantir_historico(alvo: str, cnpjs: list[str], cotas: pd.DataFrame, aviso) -> None:
    """Completa historico_mensal.csv para `alvo` até o início do fundo (aproveitando p/ os demais)."""
    status, hist = _ler_status(), _ler_hist()
    inicio_janela = cotas["DT_COMPTC"].min()
    mes_janela = inicio_janela.strftime("%Y-%m")
    # fundos que começaram dentro da janela de cotas_salvas já têm o histórico completo
    for c in cnpjs:
        d = cotas[cotas["CNPJ"] == c]
        s = status.setdefault(c, {"feitos": [], "inicio": None})
        if not d.empty and d["DT_COMPTC"].min() > inicio_janela and not s["inicio"]:
            s["inicio"] = d["DT_COMPTC"].min().strftime("%Y-%m")

    def precisa(c, per):
        s = status[c]
        chave = "".join(per)
        return chave not in s["feitos"] and (s["inicio"] is None or _ultimo_mes(per) >= s["inicio"])

    pendentes = [p for p in _periodos_antes(mes_janela) if precisa(alvo, p)]
    for i, per in enumerate(pendentes):
        if not precisa(alvo, per):  # o início pode ter sido descoberto no período anterior
            break
        aviso(i, len(pendentes), per)
        participantes = [c for c in cnpjs if precisa(c, per)]
        caminho, apagar = _arquivo_do_periodo(per)
        if caminho is None:
            df, dt_min = pd.DataFrame(columns=["CNPJ", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA"]), None
        else:
            df, dt_min = _extrair_arquivo(caminho, set(participantes))
            if apagar:
                caminho.unlink(missing_ok=True)
        novos = _agregar_mensal(df)
        hist = pd.concat([hist, novos]).drop_duplicates(["CNPJ", "ID_SUBCLASSE", "MES"], keep="last")
        for c in participantes:
            s = status[c]
            s["feitos"].append("".join(per))
            dc = df[df["CNPJ"] == c]
            if dc.empty:  # fundo ainda não existia neste período -> começa no mês seguinte
                ini = _mes_seguinte(_ultimo_mes(per))
                s["inicio"] = max(s["inicio"] or ini, ini)
            elif dt_min is not None and dc["DT_COMPTC"].min() > dt_min:  # começou dentro do período
                s["inicio"] = dc["DT_COMPTC"].min().strftime("%Y-%m")
        hist.to_csv(ARQ_HIST, index=False)
        ARQ_HIST_STATUS.write_text(json.dumps(status))


def serie_mensal(cnpj: str, sub_escolhida: str, cotas: pd.DataFrame) -> pd.DataFrame:
    """Retornos mensais do fundo (MES, DT_INI, DT_FIM, RET) juntando histórico antigo e janela recente."""
    hist = _ler_hist()
    rec = _agregar_mensal(cotas[cotas["CNPJ"] == cnpj].copy())
    todos = pd.concat([hist[hist["CNPJ"] == cnpj], rec]).drop_duplicates(["ID_SUBCLASSE", "MES"], keep="last")
    subs = set(todos["ID_SUBCLASSE"])
    # usa a subclasse escolhida; nos meses anteriores à criação das subclasses, a série sem subclasse
    esc = todos[todos["ID_SUBCLASSE"] == sub_escolhida]
    if sub_escolhida and "" in subs:
        antes = todos[(todos["ID_SUBCLASSE"] == "") & (todos["MES"] < esc["MES"].min())]
        esc = pd.concat([antes, esc])
    esc = esc.sort_values("MES").reset_index(drop=True)
    if esc.empty:
        return esc
    ret, dt_base = [], []
    for i, r in esc.iterrows():
        mesma_serie = i > 0 and esc.loc[i - 1, "ID_SUBCLASSE"] == r["ID_SUBCLASSE"]
        if mesma_serie:
            ret.append(r["COTA_FIM"] / esc.loc[i - 1, "COTA_FIM"] - 1)
            dt_base.append(esc.loc[i - 1, "DT_FIM"])
        else:  # mês de início (ou de troca de série): da primeira à última cota do mês
            ret.append(r["COTA_FIM"] / r["COTA_INI"] - 1)
            dt_base.append(r["DT_INI"])
    esc["RET"] = [x * 100 for x in ret]
    esc["DT_BASE"] = pd.to_datetime(dt_base)
    esc["DT_FIM"] = pd.to_datetime(esc["DT_FIM"])
    return esc[["MES", "DT_BASE", "DT_FIM", "RET"]]


# ---------- benchmarks de longo prazo (para o histórico)
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def bench_longo(nome: str, ini: date) -> pd.Series:
    fim = date.today()
    if nome == "CDI":
        partes, a = [], ini
        while a <= fim:  # a API do BC limita consultas diárias a 10 anos
            b = min(fim, (pd.Timestamp(a) + pd.DateOffset(years=5)).date())
            partes.append(_bcb(12, a, b))
            a = b + timedelta(days=1)
        taxas = pd.concat(partes)
        return _indice_de_taxas(taxas[~taxas.index.duplicated()])
    if nome == "Ibovespa":
        return bench_yahoo("^BVSP", ini)
    if nome == "IPCA":
        return bench_ipca(ini, fim)
    if nome == "IMA-B":
        antigos = ler_benchmarks()
        tem = antigos["IMA-B"].dropna() if antigos is not None and "IMA-B" in antigos else pd.Series(dtype="float64")
        fins = pd.date_range(ini, fim, freq="BME").tolist() + [pd.Timestamp(fim)]
        pedir = []
        for d in fins:
            d = d if d.weekday() < 5 else d - pd.offsets.BDay(1)
            if d not in tem.index:
                pedir += [d, d - pd.offsets.BDay(1), d - pd.offsets.BDay(2)]  # feriados
        with ThreadPoolExecutor(max_workers=6) as ex:
            novos = [x for x in ex.map(_imab_dia, pedir) if x]
        s = pd.concat([tem, pd.Series({d: v for d, v in novos}, dtype="float64")])
        return s[~s.index.duplicated()].sort_index()
    raise ValueError(nome)


def ret_bench(indice: pd.Series, d0, d1):
    return retorno_pct(indice, d0, d1)


# ================================================================ interface
LOGO = PASTA / "logo_itau_bba.png"
st.set_page_config(page_title="Cotas CVM · Itaú BBA", layout="wide",
                   page_icon=str(LOGO) if LOGO.exists() else None)
st.markdown(f"""<style>
  h1, h2, h3 {{ color: {AZUL_IBBA}; }}
  .secao {{ font-size: 1.6rem; font-weight: 600; color: {AZUL_IBBA};
            border-bottom: 3px solid {LARANJA_IBBA}; padding-bottom: .25rem; margin: 2rem 0 .5rem; }}
</style>""", unsafe_allow_html=True)
if LOGO.exists():
    st.image(str(LOGO), width=150)
st.title("Cotas de fundos — CVM")
st.caption("Fonte: Informe Diário CVM (dados.cvm.gov.br). Performance calculada pela variação da cota. "
           "Benchmarks: CDI e IPCA (Banco Central), IMA-B (ANBIMA), Ibovespa (Yahoo Finance).")

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
            "Subclasse": st.column_config.TextColumn(
                "Subclasse", help="Só para fundos com mais de uma subclasse. Em branco = a de maior patrimônio"),
        },
    )
    editado = editado.fillna("").astype(str).reset_index(drop=True)
    sem_cnpj = (editado["CNPJ"].map(so_digitos) == "").any()
    if ARQ_FUNDOS.exists():
        st.download_button(
            "Baixar lista de fundos (fundos.json)",
            ARQ_FUNDOS.read_bytes(),
            file_name="fundos.json",
            mime="application/json",
            help="Suba este arquivo no GitHub para a lista ficar fixa no link",
        )
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

if clicou:
    try:
        atualizar_cotas(cnpjs)
    except requests.RequestException as e:
        st.error(f"Não consegui baixar os dados da CVM: {e}")
    with st.spinner("Atualizando benchmarks… (na primeira vez o IMA-B leva cerca de 1 minuto)"):
        falhas = atualizar_benchmarks()
    if falhas:
        st.warning("Não consegui atualizar: " + ", ".join(falhas) + ". Mantive os últimos dados disponíveis.")

cotas = ler_cotas_salvas()
bench = ler_benchmarks()
faltando = set(cnpjs) - set(cotas["CNPJ"]) if cotas is not None else set(cnpjs)

with col_info:
    if ARQ_COTAS.exists():
        quando = datetime.fromtimestamp(ARQ_COTAS.stat().st_mtime)
        st.caption(f"Última atualização: {quando:%d/%m/%Y %H:%M}")
    if faltando and cotas is not None:
        st.caption("Sem dados para " + ", ".join(fmt_cnpj(c) for c in sorted(faltando))
                   + " — clique em **Atualizar cotas** (se continuar, confira o CNPJ).")
    if cotas is not None and bench is None:
        st.caption(":orange[Clique em **Atualizar cotas** para carregar os benchmarks.]")

if cotas is None:
    st.info("Clique em **Atualizar cotas** para baixar os dados pela primeira vez (leva alguns minutos).")
    st.stop()

cdi = bench["CDI"].dropna() if bench is not None and "CDI" in bench else None

# ---------- Monta séries e linhas por fundo
info = {so_digitos(r["CNPJ"]): r for r in fundos.to_dict("records")}
por_tipo: dict[str, dict] = {}
multi_sub: list = []
for cnpj in cnpjs:
    d = cotas[cotas["CNPJ"] == cnpj]
    if d.empty:
        continue
    f = info[cnpj]
    tipo = f["Estratégia"] if f["Estratégia"] in ESTRATEGIAS else "Sem classificação"
    nome_base = f["Nome"].strip() or fmt_cnpj(cnpj)
    subs = sorted(d["ID_SUBCLASSE"].unique())
    if len(subs) > 1:
        escolhida = f.get("Subclasse", "").strip()
        if escolhida not in subs:
            # padrão: subclasse com maior patrimônio na data mais recente
            if "VL_PATRIM_LIQ" in d:
                ult = d[d["DT_COMPTC"] == d["DT_COMPTC"].max()]
                escolhida = ult.sort_values("VL_PATRIM_LIQ", ascending=False)["ID_SUBCLASSE"].iloc[0]
            else:
                escolhida = subs[0]
        multi_sub.append((nome_base, escolhida, subs))
        d = d[d["ID_SUBCLASSE"] == escolhida]
    sub_usada = d["ID_SUBCLASSE"].iloc[0]
    por_tipo.setdefault(tipo, {})[nome_base] = (cnpj, d.set_index("DT_COMPTC")["VL_QUOTA"], sub_usada)

if not por_tipo:
    st.stop()

if multi_sub:
    with st.expander(f"{len(multi_sub)} fundo(s) com mais de uma subclasse — qual está sendo usada"):
        st.caption("Por padrão uso a subclasse de maior patrimônio. Para trocar, copie o código desejado "
                   "para a coluna **Subclasse** em Meus fundos.")
        for nome, esc, subs in multi_sub:
            st.markdown(f"**{nome}** — usando `{esc}` · disponíveis: " + ", ".join(f"`{x}`" for x in subs))

# ---------- Controles
c1, c2 = st.columns([3, 2])
with c1:
    tipos_disp = [t for t in ESTRATEGIAS + ["Sem classificação"] if t in por_tipo]
    filtro = st.multiselect("Estratégias", tipos_disp, placeholder="Todas")
with c2:
    periodo_graf = st.radio("Período do gráfico", ["12 meses", "YTD", "Mês"], horizontal=True)

def estilo_tabela(t: pd.DataFrame, com_cdi: bool):
    """Retorno positivo em verde, negativo em vermelho; %CDI verde se >= 100%; benchmark em cinza."""
    cols_pct = [c for c in PERIODOS if c in t]
    cols_cdi = [c for c in t if c.endswith("%CDI")]
    eh_bench = t["Fundo"].str.startswith("▸")

    def cor_retorno(v):
        if pd.isna(v):
            return ""
        return f"color: {VERDE_POS}" if v > 0 else (f"color: {VERMELHO_NEG}" if v < 0 else "")

    def cor_cdi(v):
        if pd.isna(v):
            return ""
        return f"color: {VERDE_POS}" if v >= 100 else f"color: {VERMELHO_NEG}"

    def cor_bench(linha):
        return [f"background-color: {CINZA[10]}" if eh_bench.loc[linha.name] else ""] * len(linha)

    est = (t.style.apply(cor_bench, axis=1)
           .map(cor_retorno, subset=cols_pct)
           .map(cor_cdi, subset=cols_cdi))
    if "Histórico" in t:
        est = est.map(lambda v: f"color: {LARANJA_IBBA}; font-weight: 600", subset=["Histórico"])
    return est


FMT_TABELA = {
    "Última cota": st.column_config.DateColumn(format="DD/MM/YYYY"),
    **{p: st.column_config.NumberColumn(format="%.2f%%") for p in PERIODOS},
    **{f"{p} %CDI": st.column_config.NumberColumn(format="%.0f%%") for p in PERIODOS},
}

def _fmt_pct(v, casas=2):
    return "" if v is None or pd.isna(v) else f"{v:.{casas}f}%"


def _cor(v, limite=0):
    if v is None or pd.isna(v):
        return ""
    if limite:  # % do CDI: verde se bateu o CDI
        return VERDE_POS if v >= limite else VERMELHO_NEG
    return VERDE_POS if v > 0 else (VERMELHO_NEG if v < 0 else "")


def tabela_lamina(mensal: pd.DataFrame, linhas_extra: list[tuple[str, dict, bool]]) -> tuple[str, pd.DataFrame]:
    """HTML no formato de lâmina: ano x mês, com 'No ano' e 'Acumulado'.
    linhas_extra: (rótulo, {MES: valor}, é_percentual_do_cdi)."""
    mensal = mensal.set_index("MES")
    anos = sorted({m[:4] for m in mensal.index}, reverse=True)

    def acumula(vals):
        v = [x for x in vals if x is not None and pd.notna(x)]
        return (np.prod([1 + x / 100 for x in v]) - 1) * 100 if v else None

    # acumulados desde o início, ano a ano
    linhas_base = [("Fundo", mensal["RET"].to_dict(), False)] + linhas_extra
    ret_por_rotulo = {r: d for r, d, _ in linhas_base}
    registros, html = [], []
    head = "".join(f"<th>{m}</th>" for m in MESES_PT)
    html.append(f"<table class='lamina'><thead><tr><th>Ano</th><th></th>{head}<th>No ano</th><th>Acumulado</th></tr></thead><tbody>")
    for ano in anos:
        meses = [f"{ano}-{i:02d}" for i in range(1, 13)]
        for j, (rotulo, dados, eh_cdi) in enumerate(linhas_base):
            if eh_cdi:  # % do CDI = retorno do fundo / retorno do CDI
                f, c = ret_por_rotulo["Fundo"], ret_por_rotulo["CDI"]
                cel = [(f[m] / c[m] * 100) if m in f and c.get(m) not in (None, 0) and pd.notna(c.get(m)) and c[m] > 0 else None
                       for m in meses]
                fa = acumula([f.get(m) for m in meses if m in f])
                ca = acumula([c.get(m) for m in meses if m in f])
                no_ano = fa / ca * 100 if fa is not None and ca and ca > 0 else None
                ft = acumula([v for k, v in f.items() if k <= f"{ano}-12"])
                ct = acumula([c.get(k) for k in f if k <= f"{ano}-12"])
                acum = ft / ct * 100 if ft is not None and ct and ct > 0 else None
                fmt, lim = (lambda v: _fmt_pct(v, 0)), 100
            else:
                cel = [dados.get(m) if m in mensal.index else None for m in meses]
                no_ano = acumula(cel)
                acum = acumula([dados.get(k) for k in mensal.index if k <= f"{ano}-12"])
                fmt, lim = _fmt_pct, 0
            classe = "fundo" if j == 0 else "bench"
            tds = "".join(f"<td style='color:{_cor(v, lim)}'>{fmt(v)}</td>" for v in cel + [no_ano, acum])
            primeira = f"<td class='ano' rowspan='{len(linhas_base)}'>{ano}</td>" if j == 0 else ""
            html.append(f"<tr class='{classe}'>{primeira}<td class='rot'>{rotulo}</td>{tds}</tr>")
            registros.append({"Ano": ano, "Linha": rotulo, **dict(zip(MESES_PT, cel)), "No ano": no_ano, "Acumulado": acum})
    html.append("</tbody></table>")
    return "".join(html), pd.DataFrame(registros)


CSS_LAMINA = f"""<style>
table.lamina {{ border-collapse: collapse; width: 100%; font-size: .82rem; color: {AZUL_IBBA}; }}
table.lamina th {{ background: {AZUL_IBBA}; color: #fff; font-weight: 600; padding: 6px 6px; text-align: right; }}
table.lamina th:nth-child(-n+2) {{ text-align: left; }}
table.lamina td {{ padding: 5px 6px; text-align: right; border-bottom: 1px solid {CINZA[10]}; white-space: nowrap; }}
table.lamina td.ano {{ text-align: left; font-weight: 700; vertical-align: top; border-bottom: 2px solid {CINZA[25]}; }}
table.lamina td.rot {{ text-align: left; color: {CINZA[70]}; }}
table.lamina tr.fundo td.rot {{ color: {AZUL_IBBA}; font-weight: 600; }}
table.lamina tr.bench td {{ background: #F7F7F8; }}
table.lamina td:nth-last-child(-n+2) {{ font-weight: 600; }}
</style>"""


@st.dialog("Histórico mês a mês", width="large")
def mostrar_historico(nome: str, cnpj: str, sub: str, tipo: str):
    st.markdown(f"### {nome}")
    st.caption(f"{NOME_ESTRATEGIA.get(tipo, tipo)} · CNPJ {fmt_cnpj(cnpj)}" + (f" · subclasse {sub}" if sub else ""))
    cotas_ = ler_cotas_salvas()
    status = _ler_status().get(cnpj, {})
    if not status.get("inicio"):
        st.info("Na primeira vez, o app busca o histórico na CVM desde o início do fundo. "
                "Fundos antigos podem levar alguns minutos; depois fica salvo.")
    barra = st.progress(0.0, text="Verificando histórico…")

    def aviso(i, n, per):
        rot = f"{per[1][4:]}/{per[1][:4]}" if per[0] == "M" else per[1]
        barra.progress(min(1.0, (i + 1) / max(n, 1)), text=f"Lendo arquivos da CVM: {rot}…")

    try:
        garantir_historico(cnpj, [so_digitos(c) for c in carregar_fundos()["CNPJ"]], cotas_, aviso)
    except requests.RequestException as e:
        st.warning(f"Não consegui baixar todo o histórico da CVM ({e}). Mostrando o que já está salvo.")
    barra.empty()

    mensal = serie_mensal(cnpj, sub, cotas_)
    if mensal.empty:
        st.warning("Sem dados para este fundo.")
        return

    # benchmarks mês a mês nas mesmas datas do fundo
    ini = (mensal["DT_BASE"].min() - pd.Timedelta(days=10)).date()
    extras, bench_nome, idx_bench = [], None, None
    try:
        if tipo in ("LS", "EH"):
            bench_nome = "CDI"
            idx_bench = bench_longo("CDI", ini)
        elif tipo == "LO":
            bench_nome = "Ibovespa"
            idx_bench = bench_longo("Ibovespa", ini)
        elif tipo == "LB":
            bench_nome = "IPCA + IMA-B"
            ipca, imab = bench_longo("IPCA", ini), bench_longo("IMA-B", ini)
        if bench_nome:
            vals = {}
            for r in mensal.itertuples():
                if bench_nome == "IPCA + IMA-B":
                    a, b = ret_bench(ipca, r.DT_BASE, r.DT_FIM), ret_bench(imab, r.DT_BASE, r.DT_FIM)
                    vals[r.MES] = (a or 0) + b if b is not None else None
                else:
                    vals[r.MES] = ret_bench(idx_bench, r.DT_BASE, r.DT_FIM)
            extras.append((bench_nome, vals, False))
            if bench_nome == "CDI":
                extras.append(("% CDI", {}, True))
    except Exception:  # noqa: BLE001 — sem benchmark, mostra só o fundo
        st.caption(":orange[Não consegui carregar o benchmark agora; mostrando só o fundo.]")

    # indicadores
    r = mensal["RET"] / 100
    anos = (mensal["DT_FIM"].max() - mensal["DT_BASE"].min()).days / 365.25
    acum = (np.prod(1 + r) - 1) * 100
    anual = ((1 + acum / 100) ** (1 / anos) - 1) * 100 if anos >= 1 else None
    k = st.columns(5)
    k[0].metric("Início", mensal["DT_BASE"].min().strftime("%d/%m/%Y"))
    k[1].metric("Desde o início", _fmt_pct(acum))
    k[2].metric("Ao ano", _fmt_pct(anual) if anual is not None else "–")
    k[3].metric("Meses positivos", f"{(r > 0).sum()} de {len(r)}")
    melhor, pior = mensal.loc[mensal["RET"].idxmax()], mensal.loc[mensal["RET"].idxmin()]
    k[4].metric("Melhor / pior mês", f"{melhor.RET:.1f}% / {pior.RET:.1f}%")
    if extras:
        b = [v for v in extras[0][1].values() if v is not None and pd.notna(v)]
        if b:
            acum_b = (np.prod([1 + x / 100 for x in b]) - 1) * 100
            extra = f" ({acum / acum_b * 100:.0f}% do CDI)" if bench_nome == "CDI" and acum_b > 0 else ""
            st.caption(f"{bench_nome} no mesmo período: {_fmt_pct(acum_b)}{extra}")

    html, plano = tabela_lamina(mensal, extras)
    st.markdown(CSS_LAMINA + f"<div style='overflow-x:auto'>{html}</div>", unsafe_allow_html=True)
    st.caption("Mês de início considera a variação desde a primeira cota. Mês corrente até a última cota disponível.")

    # gráfico desde o início (base 100)
    idx_f = pd.Series((1 + r).cumprod().values * 100, index=mensal["DT_FIM"])
    idx_f = pd.concat([pd.Series([100.0], index=[mensal["DT_BASE"].min()]), idx_f])
    series_b = {}
    if extras:
        rb = pd.Series([extras[0][1].get(m) for m in mensal["MES"]], index=mensal["DT_FIM"]).fillna(0) / 100
        ib = (1 + rb).cumprod() * 100
        series_b[bench_nome] = pd.concat([pd.Series([100.0], index=[mensal["DT_BASE"].min()]), ib])
    ch = grafico({nome: idx_f}, series_b, idx_f.index.min())
    if ch is not None:
        st.altair_chart(ch, width="stretch")

    st.download_button("Baixar tabela (CSV)", plano.to_csv(sep=";", decimal=",", index=False).encode("utf-8-sig"),
                       file_name=f"historico_{so_digitos(cnpj)}.csv", mime="text/csv")


def _abrir_historico(chave: str, tabela: pd.DataFrame, tipo: str):
    """Callback da tabela: clique em uma célula da linha de um fundo abre o histórico."""
    sel = st.session_state.get(chave, {}).get("selection", {})
    celulas = sel.get("cells", []) or [(r, "Fundo") for r in sel.get("rows", [])]
    if celulas:
        linha = tabela.iloc[celulas[0][0]]
        if linha.get("_cnpj"):
            st.session_state["abrir_hist"] = (linha["Fundo"], linha["_cnpj"], linha["_sub"], tipo)
    st.session_state[chave] = {"selection": {"rows": [], "columns": [], "cells": []}}


todas_series = {}

for tipo in tipos_disp:
    if filtro and tipo not in filtro:
        continue
    fundos_tipo = por_tipo[tipo]
    com_cdi = tipo in ("LS", "EH")
    nomes_bench = [b for b in BENCH_POR_TIPO.get(tipo, []) if bench is not None and b in bench]

    titulo = f"{NOME_ESTRATEGIA[tipo]} ({tipo})" if tipo in NOME_ESTRATEGIA else tipo
    st.markdown(f'<div class="secao">{titulo}</div>', unsafe_allow_html=True)
    if tipo in BENCH_POR_TIPO:
        st.caption("Benchmark: " + " e ".join(DESC_BENCH[b] for b in BENCH_POR_TIPO[tipo]))

    # tabela: fundos por YTD (maior -> menor), benchmarks no fim
    linhas = [{"Fundo": n, "_cnpj": c, "_sub": sb, **linha_performance(s, cdi, com_cdi)}
              for n, (c, s, sb) in fundos_tipo.items()]
    tab = pd.DataFrame(linhas).sort_values("YTD", ascending=False, na_position="last")
    ref = max(s.dropna().index.max() for _, s, _ in fundos_tipo.values())
    linhas_b = []
    for b in nomes_bench:
        sb = bench[b].dropna()
        sb = sb[sb.index <= ref]
        if len(sb) > 1:
            lb = linha_performance(sb, cdi, com_cdi)
            lb.update({"Fundo": f"▸ {b} (benchmark)", "_cnpj": "", "_sub": ""})
            linhas_b.append(lb)
    if linhas_b:
        tab = pd.concat([tab, pd.DataFrame(linhas_b)], ignore_index=True)

    colunas = ["Fundo", "Última cota"] + PERIODOS
    if com_cdi:
        colunas += [f"{p} %CDI" for p in PERIODOS_CDI]
    num = [c for c in colunas if c not in ("Fundo", "Última cota")]
    tab[num] = tab[num].apply(pd.to_numeric, errors="coerce")  # vazio em vez de "None"
    tab["Histórico"] = ["ver histórico ›" if c else "" for c in tab["_cnpj"]]
    tab = tab.reset_index(drop=True)
    colunas = colunas + ["Histórico"]
    chave = f"tabela_{tipo}"
    st.dataframe(estilo_tabela(tab[colunas + ["_cnpj", "_sub"]], com_cdi), hide_index=True, width="stretch",
                 column_config={**FMT_TABELA, "Histórico": st.column_config.TextColumn(
                     "Histórico", help="Clique para ver a rentabilidade mês a mês desde o início")},
                 column_order=colunas, key=chave, selection_mode="single-cell",
                 on_select=lambda k=chave, t=tab, tp=tipo: _abrir_historico(k, t, tp))

    # gráfico do tipo, com benchmark
    series_f = {n: s for n, (_, s, _) in fundos_tipo.items()}
    todas_series.update(series_f)
    j = janelas(pd.Series(0, index=[ref - pd.Timedelta(days=1), ref]))
    inicio = {"12 meses": j["12 meses"][0], "YTD": j["YTD"][0], "Mês": j["Mês (MTD)"][0]}[periodo_graf]
    series_b = {b: bench[b].dropna() for b in nomes_bench}
    ch = grafico(series_f, {k: v[v.index <= ref] for k, v in series_b.items()}, inicio)
    if ch is not None:
        st.altair_chart(ch, width="stretch")
        if series_b:
            st.caption("Linha tracejada = benchmark. Passe o mouse para ver os valores.")

st.caption("Clique no nome de um fundo (ou em **ver histórico ›**) para abrir a rentabilidade mês a mês desde o início.")
if "abrir_hist" in st.session_state:
    mostrar_historico(*st.session_state.pop("abrir_hist"))

# ---------- Download
st.divider()
hist = pd.DataFrame(todas_series)
hist.index.name = "Data"
st.download_button(
    "Baixar histórico de cotas (CSV)",
    hist.to_csv(sep=";", decimal=",").encode("utf-8-sig"),
    file_name=f"cotas_cvm_{date.today():%Y%m%d}.csv",
    mime="text/csv",
)
