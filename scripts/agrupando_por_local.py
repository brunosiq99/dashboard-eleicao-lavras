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

Os nomes alternativos mais comuns são tratados em COLUNAS_ALIAS.
"""

import json
import re
import unicodedata
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

# =============================================================================
# 1. CONFIGURAÇÕES GERAIS
# =============================================================================
ARQUIVO_CSV = "votos_por_local_votacao.csv"
ARQUIVO_CACHE_GEO = "coordenadas_cache.json"  # cache de geocodificação (opcional)
CENTRO_LAVRAS = (-21.2453, -44.9997)           # lat, lon de Lavras/MG
CIDADE_BUSCA = "Lavras, Minas Gerais, Brasil"

# Candidatos (número votável -> rótulo e cor no mapa/cards)
CAND1 = {"nr": 13, "nome": "Lula", "cor_mapa": "red", "cor_hex": "#D32F2F"}
CAND2 = {"nr": 22, "nome": "Bolsonaro", "cor_mapa": "blue", "cor_hex": "#1976D2"}
NR_BRANCO, NR_NULO = 95, 96

# Nomes alternativos de colunas aceitos (chave = nome padrão interno)
COLUNAS_ALIAS = {
    "nm_local_votacao": ["nm_local_votacao", "local_votacao", "nome_local_votacao", "local"],
    "nr_votavel": ["nr_votavel", "numero_votavel", "nr_candidato", "numero"],
    "nm_votavel": ["nm_votavel", "nome_votavel", "nm_candidato", "candidato"],
    "qt_votos": ["qt_votos", "votos", "qtd_votos", "total_votos", "qt_votos_nominais"],
    "qt_aptos": ["qt_aptos", "aptos", "eleitores_aptos", "qt_eleitores_aptos"],
    "qt_comparecimento": ["qt_comparecimento", "comparecimento", "qt_comp"],
    "qt_abstencoes": ["qt_abstencoes", "abstencoes", "faltas", "qt_faltas"],
    "latitude": ["latitude", "lat", "nr_latitude"],
    "longitude": ["longitude", "lon", "lng", "long", "nr_longitude"],
}

# -----------------------------------------------------------------------------
# DICIONÁRIO MANUAL DE COORDENADAS (garante 100% dos locais no mapa)
# Chave = nome do local EXATAMENTE como aparece no CSV (comparação sem
# acentos/maiúsculas). Preencha/ajuste com as coordenadas reais.
# Os valores abaixo são EXEMPLOS - substitua pelos reais.
# -----------------------------------------------------------------------------
COORDENADAS_MANUAIS = {
    "UNIVERSIDADE FEDERAL DE LAVRAS": (-21.2285, -44.9770),
    "ESCOLA ESTADUAL AZARIAS RIBEIRO": (-21.2457, -45.0000),
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


@st.cache_data(show_spinner="Carregando dados...")
def carregar_dados(caminho: str) -> pd.DataFrame:
    """Lê o CSV, padroniza nomes de colunas e tipos."""
    # Tenta separador ';' (padrão TSE) e depois ','
    df = None
    for sep in (";", ","):
        for enc in ("utf-8", "latin-1"):
            try:
                tmp = pd.read_csv(caminho, sep=sep, encoding=enc)
                if tmp.shape[1] > 1:
                    df = tmp
                    break
            except Exception:
                continue
        if df is not None:
            break
    if df is None:
        raise ValueError("Não foi possível ler o CSV (verifique separador/encoding).")

    # Normaliza nomes de colunas para minúsculas e aplica aliases
    df.columns = [c.strip().lower() for c in df.columns]
    renomear = {}
    for padrao, alternativas in COLUNAS_ALIAS.items():
        for alt in alternativas:
            if alt in df.columns and padrao not in df.columns:
                renomear[alt] = padrao
                break
    df = df.rename(columns=renomear)

    obrigatorias = ["nm_local_votacao", "nr_votavel", "qt_votos"]
    faltando = [c for c in obrigatorias if c not in df.columns]
    if faltando:
        raise ValueError(f"Colunas obrigatórias ausentes no CSV: {faltando}")

    df["nr_votavel"] = pd.to_numeric(df["nr_votavel"], errors="coerce")
    df["qt_votos"] = pd.to_numeric(df["qt_votos"], errors="coerce").fillna(0)
    df["nm_local_votacao"] = df["nm_local_votacao"].astype(str).str.strip()
    return df.dropna(subset=["nr_votavel"]).assign(
        nr_votavel=lambda d: d["nr_votavel"].astype(int)
    )


def rotulo_votavel(df: pd.DataFrame) -> dict:
    """Mapa nr_votavel -> nome legível (usa nm_votavel se existir)."""
    rotulos = {
        CAND1["nr"]: CAND1["nome"],
        CAND2["nr"]: CAND2["nome"],
        NR_BRANCO: "Votos Brancos",
        NR_NULO: "Votos Nulos",
    }
    if "nm_votavel" in df.columns:
        nomes = df.dropna(subset=["nm_votavel"]).drop_duplicates("nr_votavel")
        for nr, nm in zip(nomes["nr_votavel"], nomes["nm_votavel"]):
            rotulos.setdefault(int(nr), str(nm).title())
    return rotulos


def consolidar_locais(df: pd.DataFrame) -> pd.DataFrame:
    """
    Gera 1 linha por local de votação com:
    votos cand1/cand2/brancos/nulos, aptos, comparecimento, abstenções,
    vencedor e coordenadas (se vierem no CSV).

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

    # Total de votos de TODAS as linhas do local (nominais + brancos + nulos)
    total_local = df.groupby("nm_local_votacao")["qt_votos"].sum().rename("total_votos")
    locais = locais.merge(total_local, on="nm_local_votacao", how="left")

    # Métricas de eleitorado (se existirem)
    for col in ("qt_aptos", "qt_comparecimento", "qt_abstencoes"):
        if col in df.columns:
            agg = (df.assign(**{col: pd.to_numeric(df[col], errors="coerce")})
                   .groupby("nm_local_votacao")[col].max().rename(col))
            locais = locais.merge(agg, on="nm_local_votacao", how="left")

    # Fallbacks quando colunas não existem
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

    # Vencedor do local entre os dois candidatos em destaque
    locais["vencedor"] = locais.apply(
        lambda r: CAND1["nome"] if r.votos_c1 > r.votos_c2
        else CAND2["nome"] if r.votos_c2 > r.votos_c1 else "Empate",
        axis=1,
    )

    # Coordenadas vindas do CSV (se houver)
    if {"latitude", "longitude"} <= set(df.columns):
        geo = (df.assign(latitude=pd.to_numeric(df["latitude"].astype(str).str.replace(",", "."), errors="coerce"),
                         longitude=pd.to_numeric(df["longitude"].astype(str).str.replace(",", "."), errors="coerce"))
               .groupby("nm_local_votacao")[["latitude", "longitude"]].first().reset_index())
        locais = locais.merge(geo, on="nm_local_votacao", how="left")
    else:
        locais["latitude"] = pd.NA
        locais["longitude"] = pd.NA
    return locais


# =============================================================================
# 3. GEOLOCALIZAÇÃO (CSV > dicionário manual > cache/geopy > grade de reserva)
# =============================================================================
def _ler_cache_geo() -> dict:
    p = Path(ARQUIVO_CACHE_GEO)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _salvar_cache_geo(cache: dict) -> None:
    try:
        Path(ARQUIVO_CACHE_GEO).write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    except Exception:
        pass


def _geocodificar(nome: str, cache: dict):
    """Tenta geocodificar via Nominatim (se geopy instalado e houver internet)."""
    chave = normalizar_texto(nome)
    if chave in cache:
        return tuple(cache[chave]) if cache[chave] else None
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter

        geocoder = Nominatim(user_agent="dashboard_eleitoral_lavras", timeout=10)
        buscar = RateLimiter(geocoder.geocode, min_delay_seconds=1.1)
        loc = buscar(f"{nome}, {CIDADE_BUSCA}")
        cache[chave] = [loc.latitude, loc.longitude] if loc else None
        return tuple(cache[chave]) if cache[chave] else None
    except Exception:
        return None


@st.cache_data(show_spinner="Localizando locais de votação no mapa...")
def resolver_coordenadas(locais: pd.DataFrame, usar_geocodificacao: bool) -> pd.DataFrame:
    """
    Garante lat/lon para TODOS os locais. Fonte (por prioridade):
      1) colunas latitude/longitude do CSV
      2) COORDENADAS_MANUAIS
      3) cache JSON / geocodificação Nominatim (opcional)
      4) posição de reserva em espiral ao redor do centro (marcada como aproximada)
    Adiciona a coluna 'origem_coord' para transparência.
    """
    locais = locais.copy()
    manual = {normalizar_texto(k): v for k, v in COORDENADAS_MANUAIS.items()}
    cache = _ler_cache_geo()
    lats, lons, origens = [], [], []
    n_reserva = 0

    for _, r in locais.iterrows():
        nome = r["nm_local_votacao"]
        chave = normalizar_texto(nome)
        if pd.notna(r["latitude"]) and pd.notna(r["longitude"]):
            lat, lon, origem = float(r["latitude"]), float(r["longitude"]), "CSV"
        elif chave in manual:
            (lat, lon), origem = manual[chave], "Dicionário manual"
        else:
            achado = cache.get(chave) or (_geocodificar(nome, cache) if usar_geocodificacao else None)
            if achado:
                (lat, lon), origem = achado, "Geocodificação"
            else:
                # Reserva: pontos em círculo ao redor do centro (~1-2 km)
                import math
                ang = n_reserva * 2.399963  # ângulo áureo
                raio = 0.004 + 0.0015 * math.sqrt(n_reserva)
                lat = CENTRO_LAVRAS[0] + raio * math.sin(ang)
                lon = CENTRO_LAVRAS[1] + raio * math.cos(ang)
                origem = "Aproximada"
                n_reserva += 1
        lats.append(lat); lons.append(lon); origens.append(origem)

    if usar_geocodificacao:
        _salvar_cache_geo(cache)
    locais["latitude"], locais["longitude"], locais["origem_coord"] = lats, lons, origens
    return locais


# =============================================================================
# 4. COMPONENTES DE INTERFACE
# =============================================================================
def aplicar_css():
    """Pequenos ajustes de estilo para um visual mais limpo."""
    st.markdown(
        """
        <style>
        .block-container {padding-top: 2rem; padding-bottom: 2rem;}
        div[data-testid="stMetricValue"] {font-size: 2rem; font-weight: 700;}
        .cand-nome {font-size: 1.3rem; font-weight: 700; margin-bottom: .2rem;}
        .cand-sub  {color: #888; font-size: .85rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def card_candidato(coluna, cand: dict, votos: int, pct_validos: float, locais_vencidos: int, total_locais: int):
    """Card grande de destaque para um candidato."""
    with coluna:
        with st.container(border=True):
            st.markdown(
                f"<div class='cand-nome' style='color:{cand['cor_hex']}'>"
                f"● {cand['nome']} <span class='cand-sub'>({cand['nr']})</span></div>",
                unsafe_allow_html=True,
            )
            st.metric("Total de votos", fmt_int(votos))
            c1, c2 = st.columns(2)
            c1.metric("% dos votos válidos", fmt_pct(pct_validos))
            c2.metric("Locais vencidos", f"{locais_vencidos} de {total_locais}")
            st.progress(min(max(pct_validos / 100, 0.0), 1.0))


def secao_cabecalho(locais: pd.DataFrame):
    """Seção A: indicadores gerais da cidade."""
    total_c1, total_c2 = locais["votos_c1"].sum(), locais["votos_c2"].sum()
    brancos, nulos = locais["brancos"].sum(), locais["nulos"].sum()

    # Votos válidos = total de votos - brancos - nulos
    validos = locais["total_votos"].sum() - brancos - nulos
    aptos = locais["qt_aptos"].sum()
    comparecimento = locais["qt_comparecimento"].sum()
    abstencoes = locais["qt_abstencoes"].sum()
    n_locais = len(locais)

    vit_c1 = int((locais["vencedor"] == CAND1["nome"]).sum())
    vit_c2 = int((locais["vencedor"] == CAND2["nome"]).sum())

    st.title("🗳️ Dashboard Eleitoral — Lavras/MG")
    st.caption("Resultados consolidados por local de votação")

    col1, col2 = st.columns(2)
    card_candidato(col1, CAND1, total_c1, pct(total_c1, validos), vit_c1, n_locais)
    card_candidato(col2, CAND2, total_c2, pct(total_c2, validos), vit_c2, n_locais)

    k1, k2, k3, k4 = st.columns(4)
    with k1, st.container(border=True):
        st.metric("👥 Eleitores aptos", fmt_int(aptos))
    with k2, st.container(border=True):
        st.metric("🚫 Faltas / Abstenções", fmt_int(abstencoes))
        st.caption(f"{fmt_pct(pct(abstencoes, aptos))} dos aptos")
    with k3, st.container(border=True):
        st.metric("⬜ Votos brancos", fmt_int(brancos))
        st.caption(f"{fmt_pct(pct(brancos, comparecimento))} do comparecimento")
    with k4, st.container(border=True):
        st.metric("⬛ Votos nulos", fmt_int(nulos))
        st.caption(f"{fmt_pct(pct(nulos, comparecimento))} do comparecimento")


def secao_local(df: pd.DataFrame, locais: pd.DataFrame, rotulos: dict):
    """Seção B: filtro e detalhamento por local de votação."""
    st.divider()
    st.subheader("📍 Análise por local de votação")

    nomes = sorted(locais["nm_local_votacao"].unique())
    escolhido = st.selectbox("Selecione o local de votação", nomes)
    r = locais.loc[locais["nm_local_votacao"] == escolhido].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    with c1, st.container(border=True):
        st.metric(f"{CAND1['nome']} ({CAND1['nr']})", fmt_int(r.votos_c1))
        st.caption(f"{fmt_pct(pct(r.votos_c1, r.total_votos - r.brancos - r.nulos))} dos válidos")
    with c2, st.container(border=True):
        st.metric(f"{CAND2['nome']} ({CAND2['nr']})", fmt_int(r.votos_c2))
        st.caption(f"{fmt_pct(pct(r.votos_c2, r.total_votos - r.brancos - r.nulos))} dos válidos")
    with c3, st.container(border=True):
        st.metric("Comparecimento", fmt_int(r.qt_comparecimento))
        st.caption(f"{fmt_pct(pct(r.qt_comparecimento, r.qt_aptos))} dos aptos")
    with c4, st.container(border=True):
        st.metric("Abstenções (faltas)", fmt_int(r.qt_abstencoes))
        st.caption(f"{fmt_pct(pct(r.qt_abstencoes, r.qt_aptos))} dos aptos")

    with st.expander("📋 Votação completa neste local", expanded=False):
        tab = (df[df["nm_local_votacao"] == escolhido]
               .groupby("nr_votavel", as_index=False)["qt_votos"].sum()
               .sort_values("qt_votos", ascending=False))
        tab["Candidato / Voto"] = tab["nr_votavel"].map(rotulos).fillna(tab["nr_votavel"].astype(str))
        total = tab["qt_votos"].sum()
        tab["% do total"] = (tab["qt_votos"] / total * 100).round(2) if total else 0
        tab = tab.rename(columns={"nr_votavel": "Número", "qt_votos": "Votos"})
        st.dataframe(
            tab[["Candidato / Voto", "Número", "Votos", "% do total"]],
            width="stretch",
            hide_index=True,
            column_config={
                "Votos": st.column_config.NumberColumn(format="%d"),
                "% do total": st.column_config.ProgressColumn(
                    format="%.2f%%", min_value=0, max_value=100),
            },
        )
    return escolhido


def secao_mapa(locais: pd.DataFrame, local_selecionado: str):
    """Seção C: mapa Folium com pins coloridos pelo vencedor."""
    st.divider()
    st.subheader("🗺️ Mapa dos locais de votação")

    mapa = folium.Map(location=CENTRO_LAVRAS, zoom_start=13, tiles="CartoDB positron")

    for _, r in locais.iterrows():
        if r.vencedor == CAND1["nome"]:
            cor = CAND1["cor_mapa"]
        elif r.vencedor == CAND2["nome"]:
            cor = CAND2["cor_mapa"]
        else:
            cor = "gray"

        aprox = (" <i style='color:#c77'>(posição aproximada)</i>"
                 if r.origem_coord == "Aproximada" else "")
        html = (
            f"<div style='font-family:sans-serif;min-width:200px'>"
            f"<b>{r.nm_local_votacao}</b>{aprox}<br>"
            f"<b>Vencedor:</b> {r.vencedor}<br>"
            f"<span style='color:{CAND1['cor_hex']}'>{CAND1['nome']}: {fmt_int(r.votos_c1)}</span><br>"
            f"<span style='color:{CAND2['cor_hex']}'>{CAND2['nome']}: {fmt_int(r.votos_c2)}</span>"
            f"</div>"
        )
        # Destaca o local atualmente selecionado no filtro
        icone = folium.Icon(color=cor,
                            icon="star" if r.nm_local_votacao == local_selecionado else "info-sign")
        folium.Marker(
            location=(r.latitude, r.longitude),
            popup=folium.Popup(html, max_width=320),
            tooltip=f"{r.nm_local_votacao} — {r.vencedor} "
                    f"({CAND1['nome']}: {fmt_int(r.votos_c1)} | {CAND2['nome']}: {fmt_int(r.votos_c2)})",
            icon=icone,
        ).add_to(mapa)

    # Ajusta o zoom para enquadrar todos os pinos
    mapa.fit_bounds([[locais["latitude"].min(), locais["longitude"].min()],
                     [locais["latitude"].max(), locais["longitude"].max()]])

    # Legenda
    legenda = f"""
    <div style="position: fixed; bottom: 30px; left: 30px; z-index: 9999; background: white;
                padding: 10px 14px; border: 1px solid #ccc; border-radius: 8px;
                font-family: sans-serif; font-size: 13px;">
      <b>Vencedor no local</b><br>
      <span style="color:{CAND1['cor_hex']}">●</span> {CAND1['nome']}<br>
      <span style="color:{CAND2['cor_hex']}">●</span> {CAND2['nome']}
    </div>"""
    mapa.get_root().html.add_child(folium.Element(legenda))

    with st.container(border=True):
        st_folium(mapa, height=560, use_container_width=True, returned_objects=[])

    # Transparência sobre a origem das coordenadas
    aprox = locais[locais["origem_coord"] == "Aproximada"]
    if not aprox.empty:
        with st.expander(f"⚠️ {len(aprox)} local(is) com posição aproximada no mapa"):
            st.write("Adicione estes locais ao dicionário `COORDENADAS_MANUAIS` "
                     "(ou colunas latitude/longitude no CSV) para posicioná-los com exatidão:")
            st.dataframe(aprox[["nm_local_votacao"]].rename(
                columns={"nm_local_votacao": "Local de votação"}),
                width="stretch", hide_index=True)


# =============================================================================
# 5. EXECUÇÃO PRINCIPAL
# =============================================================================
def main():
    st.set_page_config(page_title="Dashboard Eleitoral - Lavras/MG",
                       page_icon="🗳️", layout="wide")
    aplicar_css()

    if not Path(ARQUIVO_CSV).exists():
        st.error(f"Arquivo **{ARQUIVO_CSV}** não encontrado na pasta do script.")
        st.stop()

    try:
        df = carregar_dados(ARQUIVO_CSV)
    except ValueError as e:
        st.error(str(e))
        st.stop()

    rotulos = rotulo_votavel(df)
    locais = consolidar_locais(df)

    with st.sidebar:
        st.header("⚙️ Opções")
        usar_geo = st.toggle(
            "Geocodificar locais sem coordenadas (internet)", value=False,
            help="Usa Nominatim/OpenStreetMap via geopy e salva resultados em cache local.")
        st.caption(f"{len(locais)} locais de votação carregados.")

    locais = resolver_coordenadas(locais, usar_geo)

    secao_cabecalho(locais)
    local_sel = secao_local(df, locais, rotulos)
    secao_mapa(locais, local_sel)


if __name__ == "__main__":
    main()