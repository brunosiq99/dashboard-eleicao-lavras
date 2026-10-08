import pandas as pd

df = pd.read_csv(
    "votos_por_local_votacao.csv",
    sep=";",
    encoding="utf-8"
)

print(df['nr_local_votacao'].nunique())