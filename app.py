"""
Cotas CVM — histórico e performance de fundos a partir do Informe Diário da CVM.
Fonte: https://dados.cvm.gov.br/dataset/fi-doc-inf_diario

Rodar:  streamlit run app.py

Arquivos salvos ao lado do app:
  fundos.json        -> lista de fundos (CNPJ, nome, estratégia)
  cotas_salvas.csv   -> últimas cotas baixadas (abre o app já com os números)
  cache_cvm/         -> arquivos mensais da CVM (evita baixar de novo)
"""
import base64
import io
import json
import re
import zipfile
from time import sleep
from concurrent.futures import ThreadPoolExecutor
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
ARQ_BENCH = PASTA / "benchmarks_v2.csv"  # v2: IMA-B oficial da ANBIMA
BENCH_POR_TIPO = {"LB": ["IPCA + IMA-B"], "LO": ["Ibovespa"], "LS": ["CDI"], "EH": ["CDI"]}
DESC_BENCH = {
    "CDI": "CDI (Banco Central)",
    "IPCA": "IPCA (IBGE via Banco Central; mês ainda não divulgado fica estável)",
    "IMA-B": "IMA-B (ANBIMA)",
    "IPCA + IMA-B": "IPCA + IMA-B (soma dos retornos diários; IMA-B da ANBIMA, IPCA do mês ainda não divulgado = 0)",
    "Ibovespa": "Ibovespa",
}
# User-Agent de navegador: a CVM (e outras fontes) devolvem 403 para o agente padrão
# do requests, principalmente a partir de servidores na nuvem como o Streamlit Cloud.
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ================================================================ utilidades
def so_digitos(cnpj) -> str:
    d = re.sub(r"\D", "", str(cnpj or ""))
    return d.zfill(14) if d else ""


def fmt_cnpj(c: str) -> str:
    c = so_digitos(c)
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if c else ""


# ================================================================ lista de fundos
# No Streamlit Cloud o disco é temporário: o que você adiciona pelo link some quando
# o app reinicia. Por isso a lista é lida e gravada no próprio repositório do GitHub.
# Configure em Settings -> Secrets:
#   [github]
#   token  = "github_pat_..."      (fine-grained, permissão Contents: Read and write)
#   repo   = "usuario/repositorio"
#   path   = "fundos.json"
#   branch = "main"
# Sem esses Secrets, o app grava em arquivo local (bom para rodar no seu computador).
COLUNAS_FUNDOS = ["CNPJ", "Nome", "Estratégia", "Subclasse"]
API_GH = "https://api.github.com"


def cfg_github() -> dict | None:
    try:
        g = st.secrets["github"]
        return {"token": g["token"], "repo": g["repo"],
                "path": g.get("path", "fundos.json"), "branch": g.get("branch", "main")}
    except Exception:  # noqa: BLE001 — sem Secrets configurados, usa o arquivo local
        return None


def _hdr_github(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


@st.cache_data(ttl=120, show_spinner=False)
def _ler_github(repo: str, path: str, branch: str, token: str) -> tuple[list, str | None]:
    """Retorna (lista de fundos, sha). sha=None quando o arquivo ainda não existe."""
    r = requests.get(f"{API_GH}/repos/{repo}/contents/{path}",
                     headers=_hdr_github(token), params={"ref": branch}, timeout=20)
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    d = r.json()
    return json.loads(base64.b64decode(d["content"]).decode("utf-8") or "[]"), d["sha"]


def _gravar_github(c: dict, dados: list) -> bool:
    conteudo = json.dumps(dados, ensure_ascii=False, indent=2) + "\n"
    corpo = {"message": f"Atualiza lista de fundos ({len(dados)} fundos)",
             "content": base64.b64encode(conteudo.encode("utf-8")).decode("ascii"),
             "branch": c["branch"]}
    if st.session_state.get("_sha_fundos"):
        corpo["sha"] = st.session_state["_sha_fundos"]
    url = f"{API_GH}/repos/{c['repo']}/contents/{c['path']}"
    r = requests.put(url, headers=_hdr_github(c["token"]), json=corpo, timeout=20)
    if r.status_code in (409, 422):  # sha desatualizado (outra aba gravou antes): relê e repete
        try:
            _ler_github.clear()
            _, sha = _ler_github(c["repo"], c["path"], c["branch"], c["token"])
            corpo["sha"] = sha
            if sha is None:
                corpo.pop("sha", None)
            r = requests.put(url, headers=_hdr_github(c["token"]), json=corpo, timeout=20)
        except requests.RequestException:
            pass
    if not r.ok:
        st.error(f"Não consegui salvar a lista no GitHub ({r.status_code}). "
                 "Confira o token e o repositório nos Secrets.")
        return False
    st.session_state["_sha_fundos"] = r.json()["content"]["sha"]
    _ler_github.clear()
    return True


def carregar_fundos() -> pd.DataFrame:
    c = cfg_github()
    if c:
        try:
            dados, sha = _ler_github(c["repo"], c["path"], c["branch"], c["token"])
            st.session_state["_sha_fundos"] = sha
        except Exception as e:  # noqa: BLE001 — GitHub fora do ar não pode derrubar o app
            st.error(f"Não consegui ler a lista de fundos no GitHub: {e}")
            dados = st.session_state.get("_fundos_cache", [])
        st.session_state["_fundos_cache"] = dados
    elif ARQ_FUNDOS.exists():
        dados = json.loads(ARQ_FUNDOS.read_text(encoding="utf-8"))
    else:
        dados = []
    df = pd.DataFrame(dados, columns=COLUNAS_FUNDOS)
    return df.fillna("").astype(str)


def salvar_fundos(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("").astype(str).copy()
    df["CNPJ"] = df["CNPJ"].map(fmt_cnpj)
    df = df[df["CNPJ"] != ""].drop_duplicates("CNPJ")
    dados = df.to_dict("records")
    c = cfg_github()
    if c:
        _gravar_github(c, dados)
        st.session_state["_fundos_cache"] = dados
    else:
        ARQ_FUNDOS.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    return df.reset_index(drop=True)


# ================================================================ download CVM
def meses_necessarios(hoje: date) -> list[str]:
    """Do mês de 13 meses atrás até o mês atual (cobre 12M, YTD e mês)."""
    fim = pd.Timestamp(hoje).to_period("M")
    return [p.strftime("%Y%m") for p in pd.period_range(fim - 13, fim, freq="M")]


def baixar_mes(ym: str, forcar: bool) -> Path | None:
    """Baixa o zip mensal da CVM. Se falhar e já houver cópia em cache, usa o cache."""
    destino = CACHE_DIR / f"inf_diario_fi_{ym}.zip"
    if destino.exists() and not forcar:
        return destino
    erro = None
    for tentativa in range(3):
        try:
            r = requests.get(BASE_URL.format(ym=ym), headers=UA, timeout=180)
            if r.status_code == 404:  # mês corrente pode ainda não ter sido publicado
                return destino if destino.exists() else None
            r.raise_for_status()
            destino.write_bytes(r.content)
            return destino
        except requests.RequestException as e:
            erro = e
            if tentativa < 2:
                sleep(2 * (tentativa + 1))
    if destino.exists():  # mês já baixado antes: segue com o que tem
        return destino
    raise erro


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
    try:
        for i, ym in enumerate(meses):
            barra.progress((i + 1) / len(meses), text=f"Lendo {ym[4:]}/{ym[:4]}…")
            # mês atual e anterior são sempre rebaixados (a CVM revisa dados recentes)
            arq = baixar_mes(ym, forcar=i >= len(meses) - 2)
            if arq:
                partes.append(ler_mes(arq, set(cnpjs)))
    finally:
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
_gh = cfg_github()
fundos = carregar_fundos()

with st.expander("Meus fundos — adicionar, renomear, classificar ou remover", expanded=fundos.empty):
    st.caption(
        "Adicione uma linha para cada fundo. Para remover, selecione a linha e aperte Delete. "
        f"As alterações são salvas automaticamente. {DESC_ESTRATEGIA}"
    )
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
    if not fundos.empty:
        st.download_button(
            "Baixar lista de fundos (fundos.json)",
            json.dumps(fundos.to_dict("records"), ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="fundos.json",
            mime="application/json",
            help="Cópia de segurança da lista",
        )
    if _gh:
        st.caption(f":green[Lista salva no GitHub:] `{_gh['repo']}` · `{_gh['path']}` (branch `{_gh['branch']}`)")
    else:
        st.caption(":orange[Atenção: a lista está sendo salva só neste servidor e some quando o app reinicia. "
                   "Configure os Secrets do GitHub para ela ficar permanente.]")
    if sem_cnpj:
        st.caption(":orange[Preencha o CNPJ da nova linha para salvar.]")
    elif not editado.equals(fundos.reset_index(drop=True)):
        salvar_fundos(editado)
        st.session_state.pop("editor_fundos", None)
        st.rerun()

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
        codigo = getattr(getattr(e, "response", None), "status_code", None)
        if codigo == 403:
            st.error("A CVM recusou o download (403). O site costuma bloquear acessos vindos de "
                     "servidores na nuvem. Tente de novo em alguns minutos; se persistir, rode o app "
                     "no seu computador para atualizar e suba o `cotas_salvas.csv` gerado.")
        else:
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
    por_tipo.setdefault(tipo, {})[nome_base] = (fmt_cnpj(cnpj), d.set_index("DT_COMPTC")["VL_QUOTA"])

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

    return (t.style.apply(cor_bench, axis=1)
            .map(cor_retorno, subset=cols_pct)
            .map(cor_cdi, subset=cols_cdi))


FMT_TABELA = {
    "Última cota": st.column_config.DateColumn(format="DD/MM/YYYY"),
    **{p: st.column_config.NumberColumn(format="%.2f%%") for p in PERIODOS},
    **{f"{p} %CDI": st.column_config.NumberColumn(format="%.0f%%") for p in PERIODOS},
}

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

    colunas = ["Fundo", "Última cota"] + PERIODOS
    if com_cdi:
        colunas += [f"{p} %CDI" for p in PERIODOS_CDI]
    num = [c for c in colunas if c not in ("Fundo", "Última cota")]
    tab[num] = tab[num].apply(pd.to_numeric, errors="coerce")  # vazio em vez de "None"
    st.dataframe(estilo_tabela(tab[colunas], com_cdi), hide_index=True, width="stretch", column_config=FMT_TABELA)

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
