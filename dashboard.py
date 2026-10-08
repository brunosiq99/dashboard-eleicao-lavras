"""
Dashboard Eleitoral - Lavras/MG
================================
Execute com:  streamlit run dashboard.py

Dependências:
    pip install streamlit pandas folium streamlit-folium
    (opcional) pip install geopy   -> geocodifica automaticamente locais sem coordenadas

Arquivo esperado na mesma pasta: votos_por_local_votacao.csv
Estrutura assumida (1 linha por local de votação x candidato/voto), com colunas
nomeadas como no padrão do TSE (maiúsculas ou minúsculas, o código normaliza):

    nm_local_votacao, nr_votavel, qt_votos
    (opcionais) nm_votavel, qt_aptos, qt_comparecimento, qt_abstencoes,
                nr_latitude / latitude, nr_longitude / longitude

DEFINIÇÕES USADAS EM TODO O DASHBOARD
    votos válidos  = votos em candidatos (exclui brancos e nulos)
    comparecimento = votos válidos + brancos + nulos
    aptos          = comparecimento + faltosos (abstenções)

COORDENADAS DOS LOCAIS (prioridade):
    1) colunas latitude/longitude no CSV de votos
    2) arquivo "coordenadas_locais.csv" (colunas: nm_local_votacao;latitude;longitude)
       -> use o botão "Baixar modelo de coordenadas" na barra lateral
    3) dicionário COORDENADAS_MANUAIS abaixo
    4) geocodificação automática (opcional, barra lateral)
    5) posição aproximada em espiral ao redor do centro (último recurso)
"""

import html as html_lib
import json
import math
import re
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus

import altair as alt
import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

# =============================================================================
# 1. CONFIGURAÇÕES GERAIS
# =============================================================================
ARQUIVO_CSV = "votos_por_local_votacao.csv"
ARQUIVO_COORDS = "coordenadas_locais.csv"      # arquivo opcional com lat/lon dos locais
ARQUIVO_CACHE_GEO = "coordenadas_cache_v2.json"  # cache de geocodificação (v2: busca por endereço)
CENTRO_LAVRAS = (-21.2453, -44.9997)           # lat, lon de Lavras/MG
CIDADE_BUSCA = "Lavras, Minas Gerais, Brasil"
# Caixa (lat/lon) do município: a busca fica restrita a ela e resultados fora dela são descartados
CAIXA_LAVRAS = ((-21.10, -45.15), (-21.40, -44.85))

# Todos os arquivos (CSV de votos, coordenadas, cache) ficam na MESMA pasta deste script
PASTA = Path(__file__).resolve().parent


def caminho(nome: str) -> Path:
    return PASTA / nome


# Candidatos (número votável -> rótulo curto e cor no mapa/cards)
CAND1 = {"nr": 13, "nome": "Lula", "cor_mapa": "red", "cor_hex": "#D32F2F"}
CAND2 = {"nr": 22, "nome": "Bolsonaro", "cor_mapa": "blue", "cor_hex": "#1976D2"}
NR_BRANCO, NR_NULO = 95, 96

# Cores das categorias nos gráficos e barras
COR_OUTROS = "#F9A825"
COR_BRANCOS = "#E0E0E0"
COR_NULOS = "#757575"
COR_FALTAS = "#546E7A"
PALETA_OUTROS = ["#8E24AA", "#F9A825", "#2E7D32", "#00838F", "#6D4C41",
                 "#E65100", "#5E35B1", "#546E7A", "#AD1457", "#9E9D24"]

# Nomes alternativos de colunas aceitos (chave = nome padrão interno)
COLUNAS_ALIAS = {
    "nm_local_votacao": ["nm_local_votacao", "local_votacao", "nome_local_votacao", "local"],
    "nr_votavel": ["nr_votavel", "numero_votavel", "nr_candidato", "numero"],
    "nm_votavel": ["nm_votavel", "nome_votavel", "nm_candidato", "candidato"],
    "qt_votos": ["qt_votos", "votos", "qtd_votos", "total_votos", "qt_votos_nominais"],
    "qt_aptos": ["qt_aptos", "aptos", "eleitores_aptos", "qt_eleitores_aptos"],
    "qt_comparecimento": ["qt_comparecimento", "comparecimento", "qt_comp"],
    "qt_abstencoes": ["qt_abstencoes", "abstencoes", "faltas", "qt_faltas"],
    "ds_local_votacao_endereco": ["ds_local_votacao_endereco", "endereco", "ds_endereco"],
    "latitude": ["latitude", "lat", "nr_latitude"],
    "longitude": ["longitude", "lon", "lng", "long", "nr_longitude"],
}

# Dicionário manual de coordenadas (EXEMPLOS - substitua ou use coordenadas_locais.csv)
COORDENADAS_MANUAIS = {
    "UNIVERSIDADE FEDERAL DE LAVRAS": (-21.2285, -44.9770),
    # "NOME DO LOCAL NO CSV": (latitude, longitude),
}


# =============================================================================
# 2. FUNÇÕES AUXILIARES
# =============================================================================
def normalizar_texto(txt: str) -> str:
    """Remove acentos, caixa e espaços extras para comparar nomes de locais."""
    txt = unicodedata.normalize("NFKD", str(txt))
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", txt).strip().upper()


def fmt_int(n) -> str:
    """Formata inteiro no padrão brasileiro (1.234.567)."""
    return f"{int(round(n)):,}".replace(",", ".")


def fmt_pct(p) -> str:
    """Formata percentual no padrão brasileiro (12,34%)."""
    return f"{p:.2f}%".replace(".", ",")


def pct(parte, total) -> float:
    return (parte / total * 100) if total else 0.0


def _ler_csv_flex(caminho: str) -> pd.DataFrame:
    """Lê CSV testando separadores ';' e ',' e encodings utf-8/latin-1."""
    for sep in (";", ","):
        for enc in ("utf-8-sig", "latin-1"):
            try:
                tmp = pd.read_csv(caminho, sep=sep, encoding=enc)
                tmp.columns = [str(c).replace("\ufeff", "") for c in tmp.columns]
                if tmp.shape[1] > 1:
                    return tmp
            except Exception:
                continue
    raise ValueError(f"Não foi possível ler '{caminho}' (verifique separador/encoding).")


@st.cache_data(show_spinner="Carregando dados...")
def carregar_dados(caminho: str) -> pd.DataFrame:
    """Lê o CSV, padroniza nomes de colunas e tipos."""
    df = _ler_csv_flex(caminho)
    df.columns = [c.strip().lower() for c in df.columns]
    renomear = {}
    for padrao, alternativas in COLUNAS_ALIAS.items():
        for alt_nome in alternativas:
            if alt_nome in df.columns and padrao not in df.columns:
                renomear[alt_nome] = padrao
                break
    df = df.rename(columns=renomear)

    faltando = [c for c in ["nm_local_votacao", "nr_votavel", "qt_votos"] if c not in df.columns]
    if faltando:
        raise ValueError(f"Colunas obrigatórias ausentes no CSV: {faltando}")

    df["nr_votavel"] = pd.to_numeric(df["nr_votavel"], errors="coerce")
    df["qt_votos"] = pd.to_numeric(df["qt_votos"], errors="coerce").fillna(0)
    df["nm_local_votacao"] = df["nm_local_votacao"].astype(str).str.strip()
    return df.dropna(subset=["nr_votavel"]).assign(
        nr_votavel=lambda d: d["nr_votavel"].astype(int)
    )


def nomes_do_csv(df: pd.DataFrame) -> dict:
    """nr_votavel -> nome completo conforme o CSV (vazio se a coluna não existir)."""
    if "nm_votavel" not in df.columns:
        return {}
    nomes = df.dropna(subset=["nm_votavel"]).drop_duplicates("nr_votavel")
    return {int(nr): str(nm).title() for nr, nm in zip(nomes["nr_votavel"], nomes["nm_votavel"])}


def rotulo_votavel(df: pd.DataFrame) -> dict:
    """Mapa nr_votavel -> nome curto (Lula/Bolsonaro) ou nome do CSV para os demais."""
    rotulos = {
        CAND1["nr"]: CAND1["nome"], CAND2["nr"]: CAND2["nome"],
        NR_BRANCO: "Votos Brancos", NR_NULO: "Votos Nulos",
    }
    for nr, nm in nomes_do_csv(df).items():
        rotulos.setdefault(nr, nm)
    return rotulos


def consolidar_locais(df: pd.DataFrame) -> pd.DataFrame:
    """
    Gera 1 linha por local de votação com votos, eleitorado, vencedor,
    percentuais sobre os votos válidos, 'outros candidatos' e margem de vitória.

    Premissa: qt_aptos / qt_comparecimento / qt_abstencoes se repetem em todas
    as linhas do mesmo local, portanto usa-se o MÁXIMO por local.
    """
    pivot = (
        df.pivot_table(index="nm_local_votacao", columns="nr_votavel",
                       values="qt_votos", aggfunc="sum", fill_value=0)
        .reindex(columns=[CAND1["nr"], CAND2["nr"], NR_BRANCO, NR_NULO], fill_value=0)
    )
    pivot.columns = ["votos_c1", "votos_c2", "brancos", "nulos"]
    locais = pivot.reset_index()

    total_local = df.groupby("nm_local_votacao")["qt_votos"].sum().rename("total_votos")
    locais = locais.merge(total_local, on="nm_local_votacao", how="left")

    for col in ("qt_aptos", "qt_comparecimento", "qt_abstencoes"):
        if col in df.columns:
            agg = (df.assign(**{col: pd.to_numeric(df[col], errors="coerce")})
                   .groupby("nm_local_votacao")[col].max().rename(col))
            locais = locais.merge(agg, on="nm_local_votacao", how="left")

    if "qt_comparecimento" not in locais:
        locais["qt_comparecimento"] = locais["total_votos"]
    locais["qt_comparecimento"] = locais["qt_comparecimento"].fillna(locais["total_votos"])
    if "qt_aptos" not in locais:
        locais["qt_aptos"] = locais["qt_comparecimento"]
    if "qt_abstencoes" not in locais:
        locais["qt_abstencoes"] = (locais["qt_aptos"] - locais["qt_comparecimento"]).clip(lower=0)
    locais["qt_abstencoes"] = locais["qt_abstencoes"].fillna(
        (locais["qt_aptos"] - locais["qt_comparecimento"]).clip(lower=0)
    )

    # Votos válidos, "outros candidatos", percentuais e margem de vitória
    locais["validos"] = locais["total_votos"] - locais["brancos"] - locais["nulos"]
    locais["votos_outros"] = (locais["validos"] - locais["votos_c1"] - locais["votos_c2"]).clip(lower=0)
    base = locais["validos"].where(locais["validos"] > 0)
    locais["pct_c1"] = (locais["votos_c1"] / base * 100).fillna(0)
    locais["pct_c2"] = (locais["votos_c2"] / base * 100).fillna(0)
    locais["pct_outros"] = (locais["votos_outros"] / base * 100).fillna(0)
    locais["margem_votos"] = (locais["votos_c1"] - locais["votos_c2"]).abs()
    locais["margem_pp"] = (locais["pct_c1"] - locais["pct_c2"]).abs()

    locais["vencedor"] = locais.apply(
        lambda r: CAND1["nome"] if r.votos_c1 > r.votos_c2
        else CAND2["nome"] if r.votos_c2 > r.votos_c1 else "Empate",
        axis=1,
    )

    # Endereço do local de votação (usado na geocodificação e nos popups do mapa)
    if "ds_local_votacao_endereco" in df.columns:
        end = (df.assign(endereco=df["ds_local_votacao_endereco"].astype(str).str.strip())
                 .query("endereco not in ('', 'nan', 'None', '#NULO#')")
                 .groupby("nm_local_votacao")["endereco"].first().reset_index())
        locais = locais.merge(end, on="nm_local_votacao", how="left")
    else:
        locais["endereco"] = None

    if {"latitude", "longitude"} <= set(df.columns):
        geo = (df.assign(latitude=pd.to_numeric(df["latitude"].astype(str).str.replace(",", "."), errors="coerce"),
                         longitude=pd.to_numeric(df["longitude"].astype(str).str.replace(",", "."), errors="coerce"))
               .groupby("nm_local_votacao")[["latitude", "longitude"]].first().reset_index())
        locais = locais.merge(geo, on="nm_local_votacao", how="left")
    else:
        locais["latitude"] = pd.NA
        locais["longitude"] = pd.NA
    return locais


def totais(sub: pd.DataFrame) -> dict:
    """Soma as categorias de votos de um conjunto de locais (cidade toda ou só os vencidos)."""
    return {
        "c1": sub["votos_c1"].sum(), "c2": sub["votos_c2"].sum(),
        "outros": sub["votos_outros"].sum(),
        "brancos": sub["brancos"].sum(), "nulos": sub["nulos"].sum(),
        "validos": sub["validos"].sum(),
        "comp": sub["qt_comparecimento"].sum(),
        "aptos": sub["qt_aptos"].sum(),
        "faltas": sub["qt_abstencoes"].sum(),
        "n": len(sub),
    }


# =============================================================================
# 3. GEOLOCALIZAÇÃO
# =============================================================================
def _ler_arquivo_coords() -> dict:
    """Lê coordenadas_locais.csv -> {NOME_NORMALIZADO: (lat, lon)}."""
    if not caminho(ARQUIVO_COORDS).exists():
        return {}
    try:
        d = _ler_csv_flex(str(caminho(ARQUIVO_COORDS)))
        d.columns = [c.strip().lower() for c in d.columns]
        d = d.rename(columns={"local": "nm_local_votacao", "lat": "latitude",
                              "lon": "longitude", "lng": "longitude"})
        for c in ("latitude", "longitude"):
            d[c] = pd.to_numeric(d[c].astype(str).str.replace(",", "."), errors="coerce")
        d = d.dropna(subset=["latitude", "longitude"])
        return {normalizar_texto(n): (la, lo) for n, la, lo in
                zip(d["nm_local_votacao"], d["latitude"], d["longitude"])}
    except Exception:
        return {}


def _ler_cache_geo() -> dict:
    p = caminho(ARQUIVO_CACHE_GEO)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _salvar_cache_geo(cache: dict) -> None:
    try:
        caminho(ARQUIVO_CACHE_GEO).write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    except Exception:
        pass


# Abreviações comuns no início de endereços do TSE
_ABREV = [
    (r"^R\.?\s+", "RUA "), (r"^AV\.?\s+", "AVENIDA "), (r"^PC[AÇ]?\.?\s+", "PRAÇA "),
    (r"^ROD\.?\s+", "RODOVIA "), (r"^TRAV\.?\s+", "TRAVESSA "), (r"^AL\.?\s+", "ALAMEDA "),
    (r"^EST\.?\s+", "ESTRADA "), (r"^LG\.?\s+", "LARGO "),
]


def limpar_endereco(end) -> str:
    """Padroniza o endereço: expande abreviações e remove 'S/N', 'Nº' e espaços/vírgulas sobrando."""
    if end is None or (not isinstance(end, str)) or not end.strip():
        return ""
    s = end.strip()
    for padrao, troca in _ABREV:
        s = re.sub(padrao, troca, s, flags=re.IGNORECASE)
    s = re.sub(r"\bS\s*/\s*N[º°]?\b\.?|\bSN\b", "", s, flags=re.IGNORECASE)  # sem número
    s = re.sub(r"\bN[º°]\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s)
    return re.sub(r"[\s,\-]+$", "", s).strip()


def montar_consultas(nome: str, endereco) -> list:
    """Lista de (tipo, consulta) da mais precisa para a menos precisa."""
    consultas = []
    end = limpar_endereco(endereco).split(" - ")[0].strip(" ,")  # descarta "- BAIRRO"/complemento
    if end:
        consultas.append(("endereço", f"{end}, {CIDADE_BUSCA}"))
        rua = re.sub(r",?\s*\d+\s*[A-Za-z]?\s*$", "", end).strip(" ,-")  # tira o número final
        if rua and rua != end:
            consultas.append(("rua", f"{rua}, {CIDADE_BUSCA}"))
    consultas.append(("nome", f"{nome}, {CIDADE_BUSCA}"))
    return consultas


def _dentro_da_caixa(lat: float, lon: float) -> bool:
    (lat1, lon1), (lat2, lon2) = CAIXA_LAVRAS
    return min(lat1, lat2) <= lat <= max(lat1, lat2) and min(lon1, lon2) <= lon <= max(lon1, lon2)


def _geocodificar(nome: str, endereco, cache: dict, buscar, Point):
    """
    Geocodifica pelo ENDEREÇO (preferencial), depois só pela rua e por último pelo nome.
    Todas as buscas ficam restritas à caixa de Lavras e o resultado é validado.
    Retorna (lat, lon, tipo) ou None. Falhas definitivas vão para o cache; erros de rede, não.
    """
    chave = normalizar_texto(limpar_endereco(endereco) or nome)
    if chave in cache:
        return tuple(cache[chave]) if cache[chave] else None
    viewbox = [Point(*CAIXA_LAVRAS[0]), Point(*CAIXA_LAVRAS[1])]
    for tipo, consulta in montar_consultas(nome, endereco):
        try:
            loc = buscar(consulta, viewbox=viewbox, bounded=True, country_codes="br", language="pt")
        except Exception:
            return None  # erro de rede: não grava no cache para tentar de novo depois
        if loc and _dentro_da_caixa(loc.latitude, loc.longitude):
            cache[chave] = [loc.latitude, loc.longitude, tipo]
            return tuple(cache[chave])
    cache[chave] = None
    return None


@st.cache_data(show_spinner="Localizando locais de votação no mapa...")
def resolver_coordenadas(locais: pd.DataFrame, usar_geocodificacao: bool, mtime_coords: float) -> pd.DataFrame:
    """
    Garante lat/lon para TODOS os locais (ver prioridades no topo do arquivo) e
    adiciona 'origem_coord'. `mtime_coords` só invalida o cache quando o arquivo muda.
    """
    locais = locais.copy()
    arquivo = _ler_arquivo_coords()
    manual = {normalizar_texto(k): v for k, v in COORDENADAS_MANUAIS.items()}
    cache = _ler_cache_geo()

    buscar, Point = None, None
    if usar_geocodificacao:
        try:
            from geopy.geocoders import Nominatim
            from geopy.extra.rate_limiter import RateLimiter
            from geopy.point import Point
            geocoder = Nominatim(user_agent="dashboard_eleitoral_lavras", timeout=10)
            buscar = RateLimiter(geocoder.geocode, min_delay_seconds=1.1)
        except ImportError:
            buscar = None  # geopy não instalado

    lats, lons, origens = [], [], []
    n_reserva = 0
    for _, r in locais.iterrows():
        nome = r["nm_local_votacao"]
        chave = normalizar_texto(nome)
        achado = None
        if pd.notna(r["latitude"]) and pd.notna(r["longitude"]):
            achado, origem = (float(r["latitude"]), float(r["longitude"])), "CSV de votos"
        elif chave in arquivo:
            achado, origem = arquivo[chave], "Arquivo de coordenadas"
        elif chave in manual:
            achado, origem = manual[chave], "Dicionário manual"
        else:
            endereco = r.get("endereco")
            chave_geo = normalizar_texto(limpar_endereco(endereco) or nome)
            resultado = tuple(cache[chave_geo]) if cache.get(chave_geo) else (
                _geocodificar(nome, endereco, cache, buscar, Point) if buscar else None)
            origem = "Geocodificação"
            if resultado:
                achado, origem = resultado[:2], f"Geocodificação: {resultado[2]}"

        if not achado:  # último recurso: espiral ao redor do centro
            ang = n_reserva * 2.399963
            raio = 0.004 + 0.0015 * math.sqrt(n_reserva)
            achado = (CENTRO_LAVRAS[0] + raio * math.sin(ang),
                      CENTRO_LAVRAS[1] + raio * math.cos(ang))
            origem = "Aproximada"
            n_reserva += 1
        lats.append(achado[0]); lons.append(achado[1]); origens.append(origem)

    if buscar:
        _salvar_cache_geo(cache)
    locais["latitude"], locais["longitude"], locais["origem_coord"] = lats, lons, origens
    return locais


# =============================================================================
# 4. COMPONENTES VISUAIS REUTILIZÁVEIS
# =============================================================================
def aplicar_css():
    st.markdown(
        """
        <style>
        .block-container {padding-top: 2rem; padding-bottom: 2rem;}
        div[data-testid="stMetricValue"] {font-size: 1.9rem; font-weight: 700;}
        .cand-nome {font-size: 1.3rem; font-weight: 700; margin-bottom: .1rem;}
        .cand-sub  {color: #888; font-size: .85rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def barra_html(segmentos: list, base: float, altura: int = 18, legenda: bool = True):
    """
    Barra horizontal empilhada em HTML. `segmentos` = [(nome, valor, cor), ...];
    cada segmento ocupa valor/base da largura. A legenda mostra o % de cada um.
    """
    partes = []
    for nome, valor, cor in segmentos:
        w = min(max(pct(valor, base), 0), 100)
        tip = html_lib.escape(f"{nome}: {fmt_int(valor)} ({fmt_pct(w)})")
        partes.append(f"<div title=\"{tip}\" style=\"width:{w:.4f}%;background:{cor};height:100%\"></div>")
    barra = (f"<div style=\"display:flex;width:100%;height:{altura}px;border-radius:8px;overflow:hidden;"
             f"background:rgba(128,128,128,.25)\">{''.join(partes)}</div>")
    leg = ""
    if legenda:
        itens = "".join(
            f"<span style=\"margin-right:18px;white-space:nowrap\">"
            f"<span style=\"display:inline-block;width:10px;height:10px;border-radius:50%;"
            f"background:{cor};margin-right:6px\"></span>{html_lib.escape(nome)} "
            f"<b>{fmt_pct(pct(valor, base))}</b></span>"
            for nome, valor, cor in segmentos)
        leg = f"<div style=\"margin-top:8px;font-size:.85rem;line-height:1.9\">{itens}</div>"
    st.markdown(barra + leg, unsafe_allow_html=True)


def barras_composicao(t: dict):
    """Duas barras: (1) votos válidos e (2) todo o eleitorado apto, a partir de `totais`."""
    st.caption("Votos válidos")
    barra_html([(CAND1["nome"], t["c1"], CAND1["cor_hex"]),
                (CAND2["nome"], t["c2"], CAND2["cor_hex"]),
                ("Outros candidatos", t["outros"], COR_OUTROS)], base=t["validos"])
    st.caption("Eleitorado apto (votos válidos, brancos, nulos e faltosos)")
    barra_html([(CAND1["nome"], t["c1"], CAND1["cor_hex"]),
                (CAND2["nome"], t["c2"], CAND2["cor_hex"]),
                ("Outros candidatos", t["outros"], COR_OUTROS),
                ("Brancos", t["brancos"], COR_BRANCOS),
                ("Nulos", t["nulos"], COR_NULOS),
                ("Faltosos", t["faltas"], COR_FALTAS)], base=t["aptos"])


def tabela_soma(t: dict) -> pd.DataFrame:
    """Tabela com cada categoria de voto e seus % sobre válidos, comparecimento e aptos."""
    def linha(nome, votos, base_v=None, base_c=None, base_a=None):
        return {
            "Categoria": nome, "Votos": votos,
            "% dos válidos": pct(votos, base_v) if base_v is not None else None,
            "% do comparecimento": pct(votos, base_c) if base_c is not None else None,
            "% dos aptos": pct(votos, base_a) if base_a is not None else None,
        }
    v, c, a = t["validos"], t["comp"], t["aptos"]
    return pd.DataFrame([
        linha(CAND1["nome"], t["c1"], v, c, a),
        linha(CAND2["nome"], t["c2"], v, c, a),
        linha("Outros candidatos", t["outros"], v, c, a),
        linha("Total de votos válidos", t["validos"], v, c, a),
        linha("Votos brancos", t["brancos"], None, c, a),
        linha("Votos nulos", t["nulos"], None, c, a),
        linha("Comparecimento", t["comp"], None, c, a),
        linha("Faltosos (abstenções)", t["faltas"], None, None, a),
        linha("Eleitores aptos", t["aptos"], None, None, a),
    ])


CONFIG_TABELA_SOMA = {
    "Votos": st.column_config.NumberColumn(format="%d"),
    "% dos válidos": st.column_config.NumberColumn(format="%.2f%%"),
    "% do comparecimento": st.column_config.NumberColumn(format="%.2f%%"),
    "% dos aptos": st.column_config.NumberColumn(format="%.2f%%"),
}


# =============================================================================
# 5. SEÇÃO A: CABEÇALHO E INDICADORES DA CIDADE
# =============================================================================
def card_candidato(coluna, cand: dict, nome_csv: str, t: dict, chave: str, vencidos: int):
    """Card de destaque: votos, locais vencidos e % sobre válidos, comparecimento e aptos."""
    votos = t[chave]
    with coluna:
        with st.container(border=True):
            st.markdown(
                f"<div class='cand-nome' style='color:{cand['cor_hex']}'>"
                f"● {cand['nome']} <span class='cand-sub'>({cand['nr']})</span></div>",
                unsafe_allow_html=True,
            )
            st.caption(nome_csv if nome_csv and normalizar_texto(nome_csv) != normalizar_texto(cand["nome"]) else " ")

            a, b = st.columns(2)
            a.metric("Total de votos", fmt_int(votos))
            b.metric("Locais vencidos", f"{vencidos} de {t['n']}")

            x, y, z = st.columns(3)
            x.metric("% dos válidos", fmt_pct(pct(votos, t["validos"])))
            y.metric("% do comparecimento", fmt_pct(pct(votos, t["comp"])))
            z.metric("% dos aptos", fmt_pct(pct(votos, t["aptos"])))

            # Barra na cor do candidato (% dos votos válidos)
            barra_html([(cand["nome"], votos, cand["cor_hex"])], base=t["validos"], legenda=False)


def secao_cabecalho(locais: pd.DataFrame, t: dict, nomes_csv: dict):
    st.title("🗳️ Dashboard Eleitoral — Lavras/MG")
    st.caption("Resultados consolidados por local de votação")

    vit_c1 = int((locais["vencedor"] == CAND1["nome"]).sum())
    vit_c2 = int((locais["vencedor"] == CAND2["nome"]).sum())

    col1, col2 = st.columns(2)
    card_candidato(col1, CAND1, nomes_csv.get(CAND1["nr"], ""), t, "c1", vit_c1)
    card_candidato(col2, CAND2, nomes_csv.get(CAND2["nr"], ""), t, "c2", vit_c2)

    # ---- Disputa na cidade: barras empilhadas + diferença + outros candidatos
    lider = CAND1 if t["c1"] > t["c2"] else CAND2
    diff = abs(t["c1"] - t["c2"])
    diff_pp = abs(pct(t["c1"], t["validos"]) - pct(t["c2"], t["validos"]))
    with st.container(border=True):
        st.markdown("**⚖️ Disputa na cidade**")
        barras_composicao(t)
        d1, d2, d3 = st.columns(3)
        d1.metric("Diferença entre os dois", fmt_int(diff))
        d1.caption(f"{lider['nome']} à frente")
        d2.metric("Diferença nos válidos", f"{diff_pp:.2f} p.p.".replace(".", ","))
        d2.caption(f"{fmt_pct(pct(diff, t['comp']))} do comparecimento")
        d3.metric("Outros candidatos", fmt_int(t["outros"]))
        d3.caption(f"{fmt_pct(pct(t['outros'], t['validos']))} dos válidos · "
                   f"{fmt_pct(pct(t['outros'], t['aptos']))} dos aptos")

    # ---- Cards secundários (todos com 2 linhas de legenda para alinhar a altura)
    k1, k2, k3, k4 = st.columns(4)
    with k1, st.container(border=True):
        st.metric("👥 Eleitores aptos", fmt_int(t["aptos"]))
        st.caption(f"Comparecimento: {fmt_int(t['comp'])}  \n{fmt_pct(pct(t['comp'], t['aptos']))} dos aptos")
    with k2, st.container(border=True):
        st.metric("🚫 Faltas / Abstenções", fmt_int(t["faltas"]))
        st.caption(f"{fmt_pct(pct(t['faltas'], t['aptos']))} dos aptos  \n"
                   f"Média de {fmt_int(t['faltas'] / max(t['n'], 1))} por local")
    with k3, st.container(border=True):
        st.metric("⬜ Votos brancos", fmt_int(t["brancos"]))
        st.caption(f"{fmt_pct(pct(t['brancos'], t['comp']))} do comparecimento  \n"
                   f"{fmt_pct(pct(t['brancos'], t['aptos']))} dos aptos")
    with k4, st.container(border=True):
        st.metric("⬛ Votos nulos", fmt_int(t["nulos"]))
        st.caption(f"{fmt_pct(pct(t['nulos'], t['comp']))} do comparecimento  \n"
                   f"{fmt_pct(pct(t['nulos'], t['aptos']))} dos aptos")


# =============================================================================
# 6. SEÇÃO B: DETALHE POR LOCAL
# =============================================================================
def legenda_pcts(valor, r) -> str:
    """Legenda de 3 linhas: % sobre válidos, comparecimento e aptos do local."""
    return (f"{fmt_pct(pct(valor, r.validos))} dos válidos  \n"
            f"{fmt_pct(pct(valor, r.qt_comparecimento))} do comparecimento  \n"
            f"{fmt_pct(pct(valor, r.qt_aptos))} dos aptos")


def cards_brancos_nulos_faltas(r, horizontal: bool):
    """Cards destacados de Brancos, Nulos e Faltosos de um local."""
    itens = [
        ("⬜ Votos brancos", r.brancos,
         f"{fmt_pct(pct(r.brancos, r.qt_comparecimento))} do comparecimento  \n"
         f"{fmt_pct(pct(r.brancos, r.qt_aptos))} dos aptos"),
        ("⬛ Votos nulos", r.nulos,
         f"{fmt_pct(pct(r.nulos, r.qt_comparecimento))} do comparecimento  \n"
         f"{fmt_pct(pct(r.nulos, r.qt_aptos))} dos aptos"),
        ("🚫 Faltosos", r.qt_abstencoes,
         f"{fmt_pct(pct(r.qt_abstencoes, r.qt_aptos))} dos aptos  \n"
         f"de {fmt_int(r.qt_aptos)} aptos"),
    ]
    alvos = st.columns(3) if horizontal else [st.container() for _ in itens]
    for alvo, (titulo, valor, legenda) in zip(alvos, itens):
        with alvo:
            with st.container(border=True):
                st.metric(titulo, fmt_int(valor))
                st.caption(legenda)


def grafico_pizza_validos(tab: pd.DataFrame):
    """Pizza (donut) dos votos válidos; a legenda traz o percentual de cada candidato."""
    d = tab.copy()
    total = d["Votos"].sum()
    d["pct"] = d["Votos"] / total * 100 if total else 0
    d["legenda"] = d["Candidato"] + " — " + d["pct"].map(fmt_pct)

    cores, i_outros = [], 0
    for nr in d["Número"]:
        if nr == CAND1["nr"]:
            cores.append(CAND1["cor_hex"])
        elif nr == CAND2["nr"]:
            cores.append(CAND2["cor_hex"])
        else:
            cores.append(PALETA_OUTROS[i_outros % len(PALETA_OUTROS)])
            i_outros += 1

    return (
        alt.Chart(d)
        .mark_arc(innerRadius=60, outerRadius=140, stroke="white", strokeWidth=1)
        .encode(
            theta=alt.Theta("Votos:Q", stack=True),
            color=alt.Color("legenda:N", sort=list(d["legenda"]),
                            scale=alt.Scale(domain=list(d["legenda"]), range=cores),
                            legend=alt.Legend(title="Votos válidos", orient="right", labelLimit=260)),
            order=alt.Order("Votos:Q", sort="descending"),
            tooltip=[alt.Tooltip("Candidato:N"),
                     alt.Tooltip("Votos:Q", format=",d"),
                     alt.Tooltip("pct:Q", format=".2f", title="% dos válidos")],
        )
        .properties(height=320)
    )


def secao_local(df: pd.DataFrame, locais: pd.DataFrame, rotulos: dict, t_cidade: dict):
    st.divider()
    st.subheader("📍 Análise por local de votação")

    nomes = sorted(locais["nm_local_votacao"].unique())
    escolhido = st.selectbox("Selecione o local de votação", nomes)
    r = locais.loc[locais["nm_local_votacao"] == escolhido].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    with c1, st.container(border=True):
        st.metric(f"{CAND1['nome']} ({CAND1['nr']})", fmt_int(r.votos_c1))
        st.caption(legenda_pcts(r.votos_c1, r))
    with c2, st.container(border=True):
        st.metric(f"{CAND2['nome']} ({CAND2['nr']})", fmt_int(r.votos_c2))
        st.caption(legenda_pcts(r.votos_c2, r))
    with c3, st.container(border=True):
        st.metric("Comparecimento", fmt_int(r.qt_comparecimento))
        st.caption(f"{fmt_pct(pct(r.qt_comparecimento, r.qt_aptos))} dos aptos  \n"
                   f"Cidade: {fmt_pct(pct(t_cidade['comp'], t_cidade['aptos']))}  \n"
                   f"{fmt_int(r.validos)} votos válidos")
    with c4, st.container(border=True):
        st.metric("Abstenções (faltas)", fmt_int(r.qt_abstencoes))
        st.caption(f"{fmt_pct(pct(r.qt_abstencoes, r.qt_aptos))} dos aptos  \n"
                   f"Cidade: {fmt_pct(pct(t_cidade['faltas'], t_cidade['aptos']))}  \n"
                   f"{fmt_int(r.qt_aptos)} eleitores aptos")

    with st.expander("📋 Votação completa neste local", expanded=True):
        visao = st.radio("Visualização", ["📊 Gráfico", "📋 Tabela"], horizontal=True,
                         label_visibility="collapsed", key="visao_local")

        # Candidatos (sem brancos/nulos), do mais ao menos votado
        tab = (df[(df["nm_local_votacao"] == escolhido)
                  & (~df["nr_votavel"].isin([NR_BRANCO, NR_NULO]))]
               .groupby("nr_votavel", as_index=False)["qt_votos"].sum()
               .sort_values("qt_votos", ascending=False))
        tab["Candidato"] = tab["nr_votavel"].map(rotulos).fillna(tab["nr_votavel"].astype(str))
        tab = tab.rename(columns={"nr_votavel": "Número", "qt_votos": "Votos"})
        tab["% dos válidos"] = [pct(v, tab["Votos"].sum()) for v in tab["Votos"]]
        tab["% do comparecimento"] = [pct(v, r.qt_comparecimento) for v in tab["Votos"]]
        tab["% dos aptos"] = [pct(v, r.qt_aptos) for v in tab["Votos"]]

        if visao == "📊 Gráfico":
            col_pizza, col_cards = st.columns([3, 1])
            with col_pizza:
                st.altair_chart(grafico_pizza_validos(tab), width="stretch")
            with col_cards:
                cards_brancos_nulos_faltas(r, horizontal=False)
        else:
            st.dataframe(
                tab[["Candidato", "Número", "Votos", "% dos válidos", "% do comparecimento", "% dos aptos"]],
                width="stretch", hide_index=True,
                column_config={
                    "Votos": st.column_config.NumberColumn(format="%d"),
                    "% dos válidos": st.column_config.ProgressColumn(format="%.2f%%", min_value=0, max_value=100),
                    "% do comparecimento": st.column_config.NumberColumn(format="%.2f%%"),
                    "% dos aptos": st.column_config.NumberColumn(format="%.2f%%"),
                },
            )
            st.markdown("**Brancos, nulos e faltosos**")
            cards_brancos_nulos_faltas(r, horizontal=True)
    return escolhido


# =============================================================================
# 7. SEÇÃO: LOCAIS VENCIDOS POR CANDIDATO
# =============================================================================
def _painel_vencedor(cand: dict, outro: dict, sub: pd.DataFrame, chave_c: str, total_votos_cand: int):
    """Conteúdo de uma aba: locais vencidos por `cand`."""
    if sub.empty:
        st.info(f"{cand['nome']} não venceu em nenhum local de votação.")
        return

    chave_o = "c2" if chave_c == "c1" else "c1"
    v_c, v_o = f"votos_{chave_c}", f"votos_{chave_o}"
    p_c, p_o = f"pct_{chave_c}", f"pct_{chave_o}"
    sub = sub.sort_values("margem_pp", ascending=False)
    t = totais(sub)

    maior, menor = sub.iloc[0], sub.iloc[-1]
    votos_nos_locais = sub[v_c].sum()

    # ---- Resumo
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1, st.container(border=True):
        st.metric("Locais vencidos", len(sub))
    with m2, st.container(border=True):
        st.metric("Votos nesses locais", fmt_int(votos_nos_locais))
        st.caption(f"{fmt_pct(pct(votos_nos_locais, total_votos_cand))} dos votos totais")
    with m3, st.container(border=True):
        st.metric("% dos válidos (média)", fmt_pct(pct(votos_nos_locais, t["validos"])))
        st.caption("ponderada pelo nº de votos")
    with m4, st.container(border=True):
        st.metric("Maior vitória", f"{maior.margem_pp:.1f} p.p.".replace(".", ","))
        st.caption(maior.nm_local_votacao)
    with m5, st.container(border=True):
        st.metric("Vitória mais apertada", f"{menor.margem_pp:.1f} p.p.".replace(".", ","))
        st.caption(menor.nm_local_votacao)

    # ---- Soma de cada categoria de voto nos locais vencidos
    with st.container(border=True):
        st.markdown(f"**➕ Soma dos votos nos {len(sub)} locais vencidos por {cand['nome']}**")
        barras_composicao(t)
        st.dataframe(tabela_soma(t), width="stretch", hide_index=True, column_config=CONFIG_TABELA_SOMA)

    # ---- Gráfico: distribuição dos válidos em cada local (cand | rival | outros candidatos)
    linhas = []
    for _, r in sub.iterrows():
        linhas += [
            (r.nm_local_votacao, cand["nome"], r[v_c], r[p_c], 0),
            (r.nm_local_votacao, outro["nome"], r[v_o], r[p_o], 1),
            (r.nm_local_votacao, "Outros candidatos", r.votos_outros, r.pct_outros, 2),
        ]
    graf = pd.DataFrame(linhas, columns=["Local", "Categoria", "Votos", "pct", "ordem"])
    ordem_locais = list(sub.sort_values(p_c, ascending=False)["nm_local_votacao"])

    barras = (
        alt.Chart(graf)
        .mark_bar()
        .encode(
            x=alt.X("pct:Q", stack="zero", title="% dos votos válidos no local",
                    scale=alt.Scale(domain=[0, 100])),
            y=alt.Y("Local:N", sort=ordem_locais, title=None, axis=alt.Axis(labelLimit=320)),
            color=alt.Color("Categoria:N",
                            scale=alt.Scale(domain=[cand["nome"], outro["nome"], "Outros candidatos"],
                                            range=[cand["cor_hex"], outro["cor_hex"], COR_OUTROS]),
                            legend=alt.Legend(orient="top", title=None)),
            order=alt.Order("ordem:Q", sort="ascending"),
            tooltip=[alt.Tooltip("Local:N"), alt.Tooltip("Categoria:N"),
                     alt.Tooltip("Votos:Q", format=",d"),
                     alt.Tooltip("pct:Q", format=".2f", title="% dos válidos")],
        )
    )
    linha50 = (alt.Chart(pd.DataFrame({"x": [50]}))
               .mark_rule(strokeDash=[4, 4], color="#888").encode(x="x:Q"))
    with st.container(border=True):
        st.markdown("**Distribuição dos votos válidos por local**")
        st.altair_chart((barras + linha50).properties(height=max(140, 26 * len(sub) + 60)), width="stretch")

    # ---- Tabela detalhada por local
    n_c, n_o = cand["nome"], outro["nome"]
    tabela = pd.DataFrame({
        "Local de votação": sub["nm_local_votacao"],
        f"% válidos {n_c}": sub[p_c],
        f"% válidos {n_o}": sub[p_o],
        "% válidos outros cand.": sub["pct_outros"],
        f"Votos {n_c}": sub[v_c],
        f"Votos {n_o}": sub[v_o],
        "Votos outros cand.": sub["votos_outros"],
        "Brancos": sub["brancos"],
        "Nulos": sub["nulos"],
        "Faltosos": sub["qt_abstencoes"],
        "Margem (votos)": sub["margem_votos"],
        "Margem (p.p.)": sub["margem_pp"],
        f"% dos aptos {n_c}": [pct(v, a) for v, a in zip(sub[v_c], sub["qt_aptos"])],
        "Aptos": sub["qt_aptos"],
        "Comparecimento": sub["qt_comparecimento"],
        "Abstenção (%)": [pct(a, b) for a, b in zip(sub["qt_abstencoes"], sub["qt_aptos"])],
    })
    barra_pct = st.column_config.ProgressColumn(format="%.2f%%", min_value=0, max_value=100)
    num = st.column_config.NumberColumn(format="%d")
    st.markdown("**Detalhe por local**")
    st.dataframe(
        tabela, width="stretch", hide_index=True,
        column_config={
            f"% válidos {n_c}": barra_pct, f"% válidos {n_o}": barra_pct,
            "% válidos outros cand.": barra_pct,
            f"Votos {n_c}": num, f"Votos {n_o}": num, "Votos outros cand.": num,
            "Brancos": num, "Nulos": num, "Faltosos": num, "Margem (votos)": num,
            "Margem (p.p.)": st.column_config.NumberColumn(format="%.2f"),
            f"% dos aptos {n_c}": st.column_config.NumberColumn(format="%.2f%%"),
            "Aptos": num, "Comparecimento": num,
            "Abstenção (%)": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )


def secao_vencedores(locais: pd.DataFrame):
    st.divider()
    st.subheader("🏆 Locais vencidos por candidato")

    v1 = locais[locais["vencedor"] == CAND1["nome"]]
    v2 = locais[locais["vencedor"] == CAND2["nome"]]
    emp = locais[locais["vencedor"] == "Empate"]

    titulos = [f"{CAND1['nome']} ({len(v1)})", f"{CAND2['nome']} ({len(v2)})"]
    if not emp.empty:
        titulos.append(f"Empates ({len(emp)})")
    abas = st.tabs(titulos)

    with abas[0]:
        _painel_vencedor(CAND1, CAND2, v1, "c1", locais["votos_c1"].sum())
    with abas[1]:
        _painel_vencedor(CAND2, CAND1, v2, "c2", locais["votos_c2"].sum())
    if not emp.empty:
        with abas[2]:
            st.dataframe(emp[["nm_local_votacao", "votos_c1", "votos_c2"]]
                         .rename(columns={"nm_local_votacao": "Local de votação",
                                          "votos_c1": f"Votos {CAND1['nome']}",
                                          "votos_c2": f"Votos {CAND2['nome']}"}),
                         width="stretch", hide_index=True)


# =============================================================================
# 8. SEÇÃO C: MAPA
# =============================================================================
def secao_mapa(locais: pd.DataFrame, local_selecionado: str):
    st.divider()
    st.subheader("🗺️ Mapa dos locais de votação")

    # OpenStreetMap é gratuito e não exige chave (o CartoDB passou a exigir API key)
    mapa = folium.Map(location=CENTRO_LAVRAS, zoom_start=13, tiles="OpenStreetMap")
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles © Esri", name="Satélite (Esri)").add_to(mapa)
    folium.TileLayer(
        tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="Map data © OpenStreetMap contributors, SRTM | Style © OpenTopoMap",
        name="Relevo (OpenTopoMap)").add_to(mapa)

    for _, r in locais.iterrows():
        cor = (CAND1["cor_mapa"] if r.vencedor == CAND1["nome"]
               else CAND2["cor_mapa"] if r.vencedor == CAND2["nome"] else "gray")
        aprox = (" <i style='color:#c77'>(posição aproximada)</i>"
                 if r.origem_coord == "Aproximada" else "")
        vit_pct = r.pct_c1 if r.vencedor == CAND1["nome"] else r.pct_c2
        end_txt = f"{r.endereco}<br>" if isinstance(r.endereco, str) and r.endereco else ""
        html = (
            f"<div style='font-family:sans-serif;min-width:200px;color:#222'>"
            f"<b>{r.nm_local_votacao}</b>{aprox}<br>"
            f"<small>{end_txt}Coordenada: {r.origem_coord}</small><br>"
            f"<b>Vencedor:</b> {r.vencedor}"
            f"{' (' + fmt_pct(vit_pct) + ' dos válidos)' if r.vencedor != 'Empate' else ''}<br>"
            f"<span style='color:{CAND1['cor_hex']}'>{CAND1['nome']}: {fmt_int(r.votos_c1)}</span><br>"
            f"<span style='color:{CAND2['cor_hex']}'>{CAND2['nome']}: {fmt_int(r.votos_c2)}</span><br>"
            f"Outros candidatos: {fmt_int(r.votos_outros)}"
            f"</div>"
        )
        icone = folium.Icon(color=cor,
                            icon="star" if r.nm_local_votacao == local_selecionado else "info-sign")
        folium.Marker(
            location=(r.latitude, r.longitude),
            popup=folium.Popup(html, max_width=320),
            tooltip=f"{r.nm_local_votacao} — {r.vencedor} "
                    f"({CAND1['nome']}: {fmt_int(r.votos_c1)} | {CAND2['nome']}: {fmt_int(r.votos_c2)})",
            icon=icone,
        ).add_to(mapa)

    mapa.fit_bounds([[locais["latitude"].min(), locais["longitude"].min()],
                     [locais["latitude"].max(), locais["longitude"].max()]])
    folium.LayerControl(collapsed=True).add_to(mapa)

    legenda = f"""
    <div style="position: fixed; bottom: 30px; left: 30px; z-index: 9999; background: #ffffff;
                color: #222222; padding: 10px 14px; border: 1px solid #ccc; border-radius: 8px;
                font-family: sans-serif; font-size: 13px;">
      <b style="color:#222">Vencedor no local</b><br>
      <span style="color:{CAND1['cor_hex']}">●</span> {CAND1['nome']}<br>
      <span style="color:{CAND2['cor_hex']}">●</span> {CAND2['nome']}
    </div>"""
    mapa.get_root().html.add_child(folium.Element(legenda))

    with st.container(border=True):
        st_folium(mapa, height=560, use_container_width=True, returned_objects=[], key="mapa_principal")

    conferir = locais[locais["origem_coord"].isin(["Geocodificação: rua", "Geocodificação: nome"])]
    if not conferir.empty:
        with st.expander(f"🔎 {len(conferir)} local(is) geocodificados com menos precisão — vale conferir"):
            st.write("Estes pontos foram achados só pela rua ou pelo nome (sem o número do endereço). "
                     "Se algum estiver errado, corrija na seção **Ajustar coordenadas** logo abaixo.")
            st.dataframe(conferir[["nm_local_votacao", "endereco", "origem_coord"]].rename(
                columns={"nm_local_votacao": "Local de votação", "endereco": "Endereço",
                         "origem_coord": "Como foi localizado"}),
                width="stretch", hide_index=True)

    aprox = locais[locais["origem_coord"] == "Aproximada"]
    if not aprox.empty:
        with st.expander(f"⚠️ {len(aprox)} local(is) com posição aproximada no mapa"):
            st.write("Posicione-os na seção **Ajustar coordenadas** logo abaixo:")
            st.dataframe(aprox[["nm_local_votacao"]].rename(
                columns={"nm_local_votacao": "Local de votação"}),
                width="stretch", hide_index=True)


# =============================================================================
# 8b. FERRAMENTA DE AJUSTE DE COORDENADAS (grava em coordenadas_locais.csv)
# =============================================================================
STATUS_REVISAR = ["Aproximada", "Geocodificação: nome", "Geocodificação: rua"]


def extrair_coordenadas(texto: str):
    """Lê 'lat, lon' colado do Google Maps (aceita ponto ou vírgula decimal). Retorna (lat, lon) ou None."""
    if not texto:
        return None
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", texto)
    if len(nums) != 2:
        return None
    try:
        lat, lon = (float(n.replace(",", ".")) for n in nums)
    except ValueError:
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _ler_coords_salvas() -> pd.DataFrame:
    """Conteúdo atual do coordenadas_locais.csv (ou DataFrame vazio)."""
    colunas = ["nm_local_votacao", "latitude", "longitude"]
    arq = caminho(ARQUIVO_COORDS)
    if arq.exists():
        try:
            d = _ler_csv_flex(str(arq))
            d.columns = [c.strip().lower() for c in d.columns]
            if all(c in d.columns for c in colunas):
                return d[colunas].astype(str)
        except Exception:
            pass
    return pd.DataFrame(columns=colunas)


def salvar_coordenada(nome: str, lat: float, lon: float) -> None:
    """Insere/atualiza a coordenada do local em coordenadas_locais.csv."""
    atual = _ler_coords_salvas()
    atual = atual[atual["nm_local_votacao"].map(normalizar_texto) != normalizar_texto(nome)]
    novo = pd.DataFrame([{"nm_local_votacao": nome, "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}"}])
    saida = pd.concat([atual, novo], ignore_index=True) if not atual.empty else novo
    saida.to_csv(caminho(ARQUIVO_COORDS), sep=";", index=False, encoding="utf-8-sig")


def remover_coordenada(nome: str) -> None:
    """Remove o ajuste manual do local (ele volta para a localização automática)."""
    atual = _ler_coords_salvas()
    atual = atual[atual["nm_local_votacao"].map(normalizar_texto) != normalizar_texto(nome)]
    atual.to_csv(caminho(ARQUIVO_COORDS), sep=";", index=False, encoding="utf-8-sig")


def secao_ajuste(locais: pd.DataFrame):
    """Escolha um local, clique no mapa (ou cole 'lat, lon') e salve em coordenadas_locais.csv."""
    st.divider()
    st.subheader("🛠️ Ajustar coordenadas dos locais")

    msg = st.session_state.pop("msg_ajuste", None)
    if msg:
        st.success(msg)

    status = dict(zip(locais["nm_local_votacao"], locais["origem_coord"]))
    n_rev = int(locais["origem_coord"].isin(STATUS_REVISAR).sum())
    st.caption(f"Os ajustes são salvos em `{caminho(ARQUIVO_COORDS)}` e têm prioridade sobre qualquer "
               f"localização automática. Pendentes ou a conferir: **{n_rev}** de {len(locais)}.")

    with st.container(border=True):
        modo = st.radio("Mostrar", [f"Pendentes e a conferir ({n_rev})", "Todos os locais"],
                        horizontal=True, index=0 if n_rev else 1, key="modo_ajuste")
        base = locais[locais["origem_coord"].isin(STATUS_REVISAR)] if (n_rev and modo.startswith("Pendentes")) else locais
        prioridade = {"Aproximada": 0, "Geocodificação: nome": 1, "Geocodificação: rua": 2}
        base = base.assign(_p=base["origem_coord"].map(prioridade).fillna(3)).sort_values(["_p", "nm_local_votacao"])
        icones = {"Aproximada": "❌", "Geocodificação: nome": "⚠️", "Geocodificação: rua": "⚠️",
                  "Arquivo de coordenadas": "✅"}
        nome = st.selectbox("Local de votação", list(base["nm_local_votacao"]),
                            format_func=lambda n: f"{icones.get(status[n], '📍')} {n}", key="local_ajuste")

        idx = list(locais["nm_local_votacao"]).index(nome)
        r = locais.iloc[idx]
        endereco = r["endereco"] if isinstance(r["endereco"], str) and r["endereco"] else ""
        ja_posicionado = r["origem_coord"] != "Aproximada"

        st.markdown(f"**Endereço:** {endereco or '—'}  \n**Situação atual:** {r['origem_coord']}")
        busca = quote_plus(f"{endereco or nome}, Lavras, MG")
        st.markdown(f"[🔗 Procurar no Google Maps](https://www.google.com/maps/search/?api=1&query={busca}) "
                    "— clique com o botão direito no local exato e clique nos números para copiá-los.")

        versao = st.session_state.get(f"v_{idx}", 0)
        texto = st.text_input("Cole aqui as coordenadas (ex.: -21.2453, -44.9997) — ou clique direto no mapa abaixo",
                              key=f"colar_{idx}_{versao}")
        colada = extrair_coordenadas(texto)
        if texto and colada is None:
            st.warning("Não consegui ler as coordenadas. Use o formato: latitude, longitude")
        escolhida = colada or st.session_state.get(f"clique_{idx}")

        # ---- mapa de ajuste
        if escolhida:
            centro, zoom = escolhida, 18
        elif ja_posicionado:
            centro, zoom = (r["latitude"], r["longitude"]), 17
        else:
            centro, zoom = CENTRO_LAVRAS, 14
        m = folium.Map(location=centro, zoom_start=zoom, tiles="OpenStreetMap")
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Tiles © Esri", name="Satélite (Esri)").add_to(m)
        if ja_posicionado:
            folium.Marker((r["latitude"], r["longitude"]), tooltip=f"Posição atual ({r['origem_coord']})",
                          icon=folium.Icon(color="orange", icon="info-sign")).add_to(m)
        if escolhida:
            folium.Marker(escolhida, tooltip="Nova posição",
                          icon=folium.Icon(color="green", icon="ok")).add_to(m)
        folium.LayerControl(collapsed=True).add_to(m)
        res = st_folium(m, height=460, use_container_width=True,
                        returned_objects=["last_clicked"], key=f"mapa_ajuste_{idx}")

        clique = (res or {}).get("last_clicked")
        if clique:
            novo = (clique["lat"], clique["lng"])
            if st.session_state.get(f"clique_{idx}") != novo:
                st.session_state[f"clique_{idx}"] = novo
                st.rerun()

        # ---- ações
        if escolhida:
            st.info(f"Nova posição: **{escolhida[0]:.6f}, {escolhida[1]:.6f}**")
            if not _dentro_da_caixa(*escolhida):
                st.warning("Esse ponto parece estar fora da região de Lavras. Confira antes de salvar.")
        else:
            st.caption("Clique no mapa ou cole as coordenadas para escolher a nova posição.")

        b1, b2, b3 = st.columns(3)
        if b1.button("💾 Salvar nova posição", type="primary", disabled=escolhida is None, key=f"salvar_{idx}"):
            salvar_coordenada(nome, *escolhida)
            st.session_state.pop(f"clique_{idx}", None)
            st.session_state[f"v_{idx}"] = versao + 1
            st.session_state["msg_ajuste"] = f"✅ Posição de **{nome}** salva em {ARQUIVO_COORDS}."
            st.rerun()
        if r["origem_coord"] in ("Geocodificação: rua", "Geocodificação: nome"):
            if b2.button("✅ Está correto, confirmar", key=f"confirmar_{idx}"):
                salvar_coordenada(nome, float(r["latitude"]), float(r["longitude"]))
                st.session_state["msg_ajuste"] = f"✅ Posição de **{nome}** confirmada."
                st.rerun()
        if r["origem_coord"] == "Arquivo de coordenadas":
            if b3.button("↩️ Remover ajuste", key=f"remover_{idx}"):
                remover_coordenada(nome)
                st.session_state["msg_ajuste"] = f"↩️ Ajuste de **{nome}** removido."
                st.rerun()


# =============================================================================
# 9. EXECUÇÃO PRINCIPAL
# =============================================================================
def main():
    st.set_page_config(page_title="Dashboard Eleitoral - Lavras/MG",
                       page_icon="🗳️", layout="wide")
    aplicar_css()

    if not caminho(ARQUIVO_CSV).exists():
        st.error(f"Arquivo **{ARQUIVO_CSV}** não encontrado na pasta do script.")
        st.stop()

    try:
        df = carregar_dados(str(caminho(ARQUIVO_CSV)))
    except ValueError as e:
        st.error(str(e))
        st.stop()

    rotulos = rotulo_votavel(df)
    nomes_csv = nomes_do_csv(df)
    locais = consolidar_locais(df)

    with st.sidebar:
        st.header("⚙️ Opções")
        usar_geo = st.toggle(
            "Geocodificar locais sem coordenadas (internet)", value=False,
            help="Usa Nominatim/OpenStreetMap via geopy (pip install geopy) e salva em cache local.")
        if st.button("Limpar cache de geocodificação"):
            caminho(ARQUIVO_CACHE_GEO).unlink(missing_ok=True)
            st.cache_data.clear()
            st.rerun()
        st.caption(f"{len(locais)} locais de votação carregados.")

    mtime = caminho(ARQUIVO_COORDS).stat().st_mtime if caminho(ARQUIVO_COORDS).exists() else 0.0
    locais = resolver_coordenadas(locais, usar_geo, mtime)

    pendentes = locais[locais["origem_coord"] == "Aproximada"]
    if not pendentes.empty:
        modelo = pendentes[["nm_local_votacao"]].assign(latitude="", longitude="")
        with st.sidebar:
            st.warning(f"{len(pendentes)} local(is) sem coordenadas. Use a seção "
                       "**Ajustar coordenadas** no final da página.")
            st.download_button(
                "⬇️ Baixar modelo de coordenadas",
                data=modelo.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name=ARQUIVO_COORDS, mime="text/csv",
                help=f"Preencha latitude/longitude (ex.: -21,2453) e salve como {ARQUIVO_COORDS} "
                     "na mesma pasta do dashboard.")

    t_cidade = totais(locais)
    secao_cabecalho(locais, t_cidade, nomes_csv)
    local_sel = secao_local(df, locais, rotulos, t_cidade)
    secao_vencedores(locais)
    secao_mapa(locais, local_sel)
    secao_ajuste(locais)


if __name__ == "__main__":
    main()
