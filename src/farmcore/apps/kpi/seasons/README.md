# Backtest weather extract (France, station 33394002)

`meteofrance_33394002_2020_2023_marsep.csv`: hourly rows of Météo-France station 33394002
(ST EMILION), 1 Mar – 30 Sep of 2020, 2021, 2022 and 2023 (UTC), 20 544 rows, same columns and
units as `sample-data/meteofrance/demo_station_33394002_2024_2025.csv`.

- **Source:** Météo-France, *Données climatologiques de base - horaires*, département 33,
  `H_33_previous-2020-2024.csv.gz`
  (<https://object.files.data.gouv.fr/meteofrance/data/synchro_ftp/BASE/HOR/H_33_previous-2020-2024.csv.gz>,
  31 584 566 bytes, SHA-256 `320f47cd87426cb39415665994afa7a046aa16f4c5a7aefb8a82fbccad3da082`,
  downloaded 17 Oct 2026). An earlier download of the same file (same name, same size,
  fetched 16 Oct 2026) has a different SHA-256,
  `232367eddf5994217964f2a2fdb49ce57e5813a8b83fc6253b2dbc351f5d8675`: the source file is rebuilt
  as it is updated. **The extract was verified row by row:** filtering both archives with the
  same script gives byte-identical output (20 545 lines including the header, 0 differing rows),
  so the extract does not depend on which copy is used. Dataset: <https://www.data.gouv.fr/fr/datasets/donnees-climatologiques-de-base-horaires/>
- **Licence:** Licence Ouverte / Etalab 2.0 (attribution: Météo-France).
- **Filter:** `NUM_POSTE = 33394002`, `AAAAMMJJHH` in Mar–Sep of 2020–2023; `RR1`→`rr1_mm`,
  `T`→`t_c`, `U`→`rh_pct`, `FF`→`ff_ms` (`ff_kmh` = `ff_ms` × 3.6, 1 decimal), `FXI`→`fxi_ms`.
  Empty source cells stay empty; nothing is filled or interpolated.
- **Known gaps (counted by `farmcore backtest`, never filled):** 2021 has no rain value for
  27 Apr 07:00 – 30 Apr 06:00 UTC (72 h, before that season's first alert). 2023 has 205 h
  without humidity (`rh_pct`; 1 Mar – 11 Apr, all before the 15 Apr season gate) and 2022 has 1 h
  (8 Apr); the incubation step skips an hour without humidity.
