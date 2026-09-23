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
from datetime import date, datetime, timedelta
from pathlib import Path

import altair as alt
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
ARQ_BENCH = PASTA / "benchmarks.csv"
BENCH_POR_TIPO = {"LB": ["IPCA + IMA-B"], "LO": ["Ibovespa"], "LS": ["CDI"], "EH": ["CDI"]}
DESC_BENCH = {
    "CDI": "CDI (Banco Central)",
    "IPCA": "IPCA (IBGE via Banco Central; mês ainda não divulgado fica estável)",
    "IMA-B": "IMA-B (via ETF IMAB11)",
    "IPCA + IMA-B": "IPCA + IMA-B (soma dos retornos diários; IMA-B via ETF IMAB11, IPCA do mês ainda não divulgado = 0)",
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


def atualizar_benchmarks() -> list[str]:
    """Baixa os benchmarks e grava benchmarks.csv. Retorna a lista de falhas."""
    fim = date.today()
    ini = (pd.Timestamp(fim) - pd.DateOffset(months=14)).date()
    fontes = {
        "CDI": lambda: bench_cdi(ini, fim),
        "IPCA": lambda: bench_ipca(ini, fim),
        "IMA-B": lambda: bench_yahoo("IMAB11.SA", ini),
        "Ibovespa": lambda: bench_yahoo("^BVSP", ini),
    }
    antigos = ler_benchmarks()
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
# Paleta categórica em ordem fixa; benchmarks em cinza tracejado.
PALETA = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CORES_BENCH = ["#3d3d3a", "#8a8980"]


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
        x=alt.X("Data:T", title=None, axis=alt.Axis(format="%b/%y", grid=False, tickCount="month")),
        y=alt.Y("Valor:Q", title="Base 100", scale=alt.Scale(zero=False),
                axis=alt.Axis(gridColor="#ecebe6", gridDash=[2, 2])),
        color=alt.Color("Série:N", scale=alt.Scale(domain=dominio, range=cores),
                        legend=alt.Legend(title=None, orient="bottom", columns=3, labelLimit=260)),
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
    regua = alt.Chart(df).mark_rule(color="#b5b4ad").encode(x="Data:T").transform_filter(perto)
    return (linhas_ch + alvo + pontos + regua).properties(height=340)


# ================================================================ interface
st.set_page_config(page_title="Cotas CVM", layout="wide")
st.title("Cotas de fundos — CVM")
st.caption("Fonte: Informe Diário CVM (dados.cvm.gov.br). Performance calculada pela variação da cota. "
           "Benchmarks: CDI e IPCA (Banco Central), Ibovespa e IMA-B (Yahoo Finance; IMA-B via ETF IMAB11).")

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
    with st.spinner("Atualizando benchmarks…"):
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
for cnpj in cnpjs:
    d = cotas[cotas["CNPJ"] == cnpj]
    if d.empty:
        continue
    f = info[cnpj]
    tipo = f["Estratégia"] if f["Estratégia"] in ESTRATEGIAS else "Sem classificação"
    nome_base = f["Nome"].strip() or fmt_cnpj(cnpj)
    for sub, g in d.groupby("ID_SUBCLASSE"):
        nome = nome_base + (f" · {sub}" if sub else "")
        por_tipo.setdefault(tipo, {})[nome] = (fmt_cnpj(cnpj), g.set_index("DT_COMPTC")["VL_QUOTA"])

if not por_tipo:
    st.stop()

# ---------- Controles
c1, c2 = st.columns([3, 2])
with c1:
    tipos_disp = [t for t in ESTRATEGIAS + ["Sem classificação"] if t in por_tipo]
    filtro = st.multiselect("Estratégias", tipos_disp, placeholder="Todas")
with c2:
    periodo_graf = st.radio("Período do gráfico", ["12 meses", "YTD", "Mês"], horizontal=True)

pct = st.column_config.NumberColumn(format="%.2f%%")
pct_cdi = st.column_config.NumberColumn(format="%.0f%%")
todas_series = {}

for tipo in tipos_disp:
    if filtro and tipo not in filtro:
        continue
    fundos_tipo = por_tipo[tipo]
    com_cdi = tipo in ("LS", "EH")
    nomes_bench = [b for b in BENCH_POR_TIPO.get(tipo, []) if bench is not None and b in bench]

    titulo = f"{NOME_ESTRATEGIA[tipo]} ({tipo})" if tipo in NOME_ESTRATEGIA else tipo
    st.header(titulo, divider="gray")
    if tipo in BENCH_POR_TIPO:
        st.caption("Benchmark: " + " e ".join(DESC_BENCH[b] for b in BENCH_POR_TIPO[tipo]))

    # tabela: fundos por YTD (maior -> menor), benchmarks no fim
    linhas = [{"Fundo": n, "CNPJ": c, **linha_performance(s, cdi, com_cdi)} for n, (c, s) in fundos_tipo.items()]
    tab = pd.DataFrame(linhas).sort_values("YTD", ascending=False, na_position="last")
    ref = max(s.dropna().index.max() for _, s in fundos_tipo.values())
    linhas_b = []
    for b in nomes_bench:
        sb = bench[b].dropna()
        sb = sb[sb.index <= ref]
        if len(sb) > 1:
            lb = linha_performance(sb, cdi, com_cdi)
            lb.update({"Fundo": f"▸ {b} (benchmark)", "CNPJ": "", "Cota": float("nan")})
            linhas_b.append(lb)
    if linhas_b:
        tab = pd.concat([tab, pd.DataFrame(linhas_b)], ignore_index=True)

    colunas = ["Fundo", "CNPJ", "Última cota", "Cota"] + PERIODOS
    if com_cdi:
        colunas += [f"{p} %CDI" for p in PERIODOS]
    cfg = {"Última cota": st.column_config.DateColumn(format="DD/MM/YYYY"),
           "Cota": st.column_config.NumberColumn(format="%.6f"),
           **{p: pct for p in PERIODOS},
           **{f"{p} %CDI": pct_cdi for p in PERIODOS}}
    st.dataframe(tab[colunas], hide_index=True, width="stretch", column_config=cfg)

    # gráfico do tipo, com benchmark
    series_f = {n: s for n, (_, s) in fundos_tipo.items()}
    todas_series.update(series_f)
    j = janelas(pd.Series(0, index=[ref - pd.Timedelta(days=1), ref]))
    inicio = {"12 meses": j["12 meses"][0], "YTD": j["YTD"][0], "Mês": j["Mês (MTD)"][0]}[periodo_graf]
    series_b = {b: bench[b].dropna() for b in nomes_bench}
    ch = grafico(series_f, {k: v[v.index <= ref] for k, v in series_b.items()}, inicio)
    if ch is not None:
        st.altair_chart(ch, width="stretch")
        if series_b:
            st.caption("Linha tracejada = benchmark. Passe o mouse para ver os valores.")

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
