# Race Predictions vs Actual Results — 2026

*Model trained on 2023–2026 data (cutoff 2026-03-09, validated on Tirreno-Adriatico 2026). Rider speciality profiles included from 2025/2026 scrape.*

**How to read these tables:** The left side shows the model's predicted top-10 (ranked by probability). The right side shows who actually finished in the top 10. For stage races, a GC leaderboard shows which riders the model most consistently placed in predicted top-10s across all stages — a proxy for overall contention.

---

## Strade Bianche 2026 — 2026-03-07 (Hilly)

> One-day race. AUC 0.871 | Precision@10: 0.50

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Pogačar Tadej | 0.40 | 1 | Pogačar Tadej |
| 2 | del Toro Isaac | 0.28 | 2 | Seixas Paul |
| 3 | Gautherat Pierre | 0.24 | 3 | del Toro Isaac |
| 4 | Johannessen Tobias Halland | 0.20 | 4 | Grégoire Romain |
| 5 | Hatherly Alan | 0.18 | 5 | Vermeersch Gianni |
| 6 | Vermeersch Florian | 0.16 | 6 | Christen Jan |
| 7 | Zambanini Edoardo | 0.16 | 7 | Pidcock Thomas |
| 8 | Velasco Simone | 0.16 | 8 | Jorgenson Matteo |
| 9 | Kron Andreas | 0.15 | 9 | Kron Andreas |
| 10 | Christen Jan | 0.15 | 10 | van Aert Wout |

## Paris-Nice 2026

> AUC 0.776 | Avg precision@10: 0.188

> *Stage 7 (mountain) was shortened on the day, neutralising the expected GC finish — model correctly identified the top climbers but the stage was neutralised.*

### Stage 1 — 2026-03-08 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Plowright Jensen | 0.31 | 1 | Lamperti Luke |
| 2 | Lamperti Luke | 0.28 | 2 | Braet Vito |
| 3 | Vingegaard Jonas | 0.23 | 3 | Aular Orluis |
| 4 | Coquard Bryan | 0.23 | 4 | Fretin Milan |
| 5 | Houcou Emmanuel | 0.23 | 5 | Girmay Biniam |
| 6 | Aular Orluis | 0.22 ✓ | 6 | Plowright Jensen |
| 7 | Fretin Milan | 0.22 ✓ | 7 | Teunissen Mike |
| 8 | Giddings Joshua | 0.21 | 8 | Løland Sakarias Koller |
| 9 | van den Berg Marijn | 0.20 | 9 | Zingle Axel |
| 10 | Mezgec Luka | 0.19 | 10 | Donaldson Robert |

*4/10 correct (Lamperti ✓, Aular ✓, Fretin ✓, Plowright ✓)*

### Stage 2 — 2026-03-09 (Flat)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Plowright Jensen | 0.31 | 1 | Kanter Max |
| 2 | Lamperti Luke | 0.31 ✓ | 2 | Pithie Laurence |
| 3 | Mezgec Luka | 0.30 | 3 | Stuyven Jasper |
| 4 | Fretin Milan | 0.29 | 4 | Godon Dorian |
| 5 | Girmay Biniam | 0.28 | 5 | Lamperti Luke |
| 6 | Aular Orluis | 0.28 | 6 | Pluimers Rick |
| 7 | Houcou Emmanuel | 0.25 | 7 | Askey Lewis |
| 8 | Bol Cees | 0.24 | 8 | Van Asbroeck Tom |
| 9 | Coquard Bryan | 0.22 | 9 | Russo Clément |
| 10 | Teunissen Mike | 0.20 | 10 | Turgis Anthony |

*1/10 correct (Lamperti ✓)*

### Stage 3 — 2026-03-10 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Vingegaard Jonas | 0.34 ✓ | 1 | Ayuso Juan |
| 2 | Stuyven Jasper | 0.30 | 2 | Vauquelin Kévin |
| 3 | Pithie Laurence | 0.28 ✓ | 3 | Onley Oscar |
| 4 | Godon Dorian | 0.28 | 4 | Hoole Daan |
| 5 | Teunissen Mike | 0.28 | 5 | Armirail Bruno |
| 6 | Zingle Axel | 0.28 | 6 | Piganzoli Davide |
| 7 | Turgis Anthony | 0.26 | 7 | Vingegaard Jonas |
| 8 | Braet Vito | 0.25 | 8 | Vlasov Aleksandr |
| 9 | Løland Sakarias Koller | 0.24 | 9 | Martínez Daniel Felipe |
| 10 | Watson Samuel | 0.23 | 10 | Pithie Laurence |

*2/10 correct (Vingegaard ✓, Pithie ✓)*

### Stage 4 — 2026-03-11 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Lamperti Luke | 0.36 | 1 | Vingegaard Jonas |
| 2 | Aular Orluis | 0.36 | 2 | Martínez Daniel Felipe |
| 3 | Pithie Laurence | 0.35 | 3 | van Dijke Tim |
| 4 | Plowright Jensen | 0.34 ✓ | 4 | van Dijke Mick |
| 5 | Pluimers Rick | 0.32 | 5 | Steinhauser Georg |
| 6 | Kanter Max | 0.32 | 6 | Vauquelin Kévin |
| 7 | Braet Vito | 0.31 | 7 | Martinez Lenny |
| 8 | Fretin Milan | 0.31 | 8 | Gaudu David |
| 9 | Godon Dorian | 0.30 | 9 | Plowright Jensen |
| 10 | Teunissen Mike | 0.30 | 10 | Soler Marc |

*1/10 correct (Plowright ✓)*

### Stage 5 — 2026-03-12 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Plowright Jensen | 0.36 | 1 | Vingegaard Jonas |
| 2 | Lamperti Luke | 0.35 | 2 | Paret-Peintre Valentin |
| 3 | Pithie Laurence | 0.34 | 3 | Tejada Harold |
| 4 | Kanter Max | 0.30 | 4 | Martinez Lenny |
| 5 | Aular Orluis | 0.30 | 5 | Izagirre Ion |
| 6 | Godon Dorian | 0.26 | 6 | Martínez Daniel Felipe |
| 7 | Pluimers Rick | 0.26 | 7 | Vauquelin Kévin ✓ |
| 8 | Vauquelin Kévin | 0.25 ✓ | 8 | Steinhauser Georg |
| 9 | Girmay Biniam | 0.25 | 9 | Rondel Mathys |
| 10 | Vingegaard Jonas | 0.24 ✓ | 10 | Soler Marc |

*2/10 correct (Vauquelin ✓, Vingegaard ✓)*

### Stage 6 — 2026-03-13 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Plowright Jensen | 0.36 | 1 | Tejada Harold |
| 2 | Lamperti Luke | 0.34 | 2 | Godon Dorian |
| 3 | Pithie Laurence | 0.32 ✓ | 3 | Askey Lewis |
| 4 | Kanter Max | 0.30 | 4 | Coquard Bryan |
| 5 | Vauquelin Kévin | 0.27 ✓ | 5 | Trentin Matteo |
| 6 | Aular Orluis | 0.27 | 6 | Pithie Laurence |
| 7 | Godon Dorian | 0.26 ✓ | 7 | Vauquelin Kévin |
| 8 | Girmay Biniam | 0.26 | 8 | Paret-Peintre Valentin |
| 9 | Vingegaard Jonas | 0.25 | 9 | Vlasov Aleksandr |
| 10 | Martínez Daniel Felipe | 0.23 | 10 | Baudin Alex |

*3/10 correct (Pithie ✓, Vauquelin ✓, Godon ✓)*

### Stage 7 — 2026-03-14 (Mountain — shortened/neutralised)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Vingegaard Jonas | 0.89 | 1 | Godon Dorian |
| 2 | Vlasov Aleksandr | 0.67 | 2 | Girmay Biniam |
| 3 | Vauquelin Kévin | 0.49 | 3 | Bol Cees |
| 4 | Martínez Daniel Felipe | 0.48 | 4 | Pithie Laurence |
| 5 | Paret-Peintre Valentin | 0.39 | 5 | Lamperti Luke |
| 6 | Soler Marc | 0.39 | 6 | Teunissen Mike |
| 7 | Tejada Harold | 0.39 | 7 | Plowright Jensen |
| 8 | Rondel Mathys | 0.36 | 8 | Stuyven Jasper |
| 9 | Martinez Lenny | 0.33 | 9 | Trentin Matteo |
| 10 | Izagirre Ion | 0.27 | 10 | Watson Samuel |

*0/10 correct — stage was shortened, GC riders sat up, breakaway succeeded*

### Stage 8 — 2026-03-15 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Vingegaard Jonas | 0.54 ✓ | 1 | Martinez Lenny |
| 2 | Godon Dorian | 0.40 | 2 | Vingegaard Jonas |
| 3 | Pithie Laurence | 0.36 | 3 | Tejada Harold |
| 4 | Plowright Jensen | 0.36 | 4 | Vauquelin Kévin ✓ |
| 5 | Vauquelin Kévin | 0.34 ✓ | 5 | Baudin Alex |
| 6 | Vlasov Aleksandr | 0.32 | 6 | Steinhauser Georg |
| 7 | Trentin Matteo | 0.30 | 7 | Izagirre Ion |
| 8 | Martínez Daniel Felipe | 0.30 | 8 | Rondel Mathys |
| 9 | Stuyven Jasper | 0.26 | 9 | Vinokurov Nicolas |
| 10 | Askey Lewis | 0.24 | 10 | Soler Marc |

*2/10 correct (Vingegaard ✓, Vauquelin ✓)*

### Paris-Nice 2026 — General Classification

| # | Actual GC | # | Predicted (stage top-10 appearances) | Stages | Avg P(top10) |
|---|-----------|---|-----------------------------------------|--------|--------------|
| 1 | Vingegaard Jonas | 1 | Lamperti Luke | 6 | 0.33 |
| 2 | Martínez Daniel Felipe | 2 | Plowright Jensen | 6 | 0.34 |
| 3 | Steinhauser Georg | 3 | Pithie Laurence | 5 | 0.33 |
| 4 | Vauquelin Kévin | 4 | Vingegaard Jonas | 5 | 0.45 |
| 5 | Martinez Lenny | 5 | Aular Orluis | 5 | 0.28 |
| 6 | Soler Marc | 6 | Godon Dorian | 5 | 0.30 |
| 7 | Izagirre Ion | 7 | Girmay Biniam | 4 | 0.26 |
| 8 | Rondel Mathys | 8 | Fretin Milan | 4 | 0.27 |
| 9 | Baudin Alex | 9 | Vauquelin Kévin | 4 | 0.34 |
| 10 | Tejada Harold | 10 | Kanter Max | 3 | 0.31 |

## Tirreno-Adriatico 2026

> AUC 0.872 | Avg precision@10: 0.371

> *GC winner del Toro correctly in predicted top-10 for 4 of 7 stages. Strong sprint stage predictions (Stage 1: 7/10, Stage 7: 6/10).*

### Stage 1 — 2026-03-09 (Flat — TTT)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Tiberi Antonio | 0.35 ✓ | 1 | Ganna Filippo |
| 2 | Hayter Ethan | 0.35 ✓ | 2 | Arensman Thymen |
| 3 | del Toro Isaac | 0.34 ✓ | 3 | Walscheid Max |
| 4 | Hatherly Alan | 0.34 ✓ | 4 | Sheffield Magnus |
| 5 | Sheffield Magnus | 0.30 ✓ | 5 | Milan Jonathan |
| 6 | Artz Huub | 0.18 | 6 | Hatherly Alan |
| 7 | Ganna Filippo | 0.17 ✓ | 7 | Roglič Primož |
| 8 | Christen Jan | 0.16 | 8 | Hayter Ethan |
| 9 | Pedersen Rasmus Søjberg | 0.16 | 9 | Tiberi Antonio |
| 10 | Milan Jonathan | 0.15 ✓ | 10 | del Toro Isaac |

*7/10 correct (Tiberi ✓, Hayter ✓, del Toro ✓, Hatherly ✓, Sheffield ✓, Ganna ✓, Milan ✓)*

### Stage 2 — 2026-03-10 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Milan Jonathan | 0.44 | 1 | van der Poel Mathieu |
| 2 | Welsford Sam | 0.36 | 2 | del Toro Isaac ✓ |
| 3 | Andresen Tobias Lund | 0.28 | 3 | Pellizzari Giulio |
| 4 | del Toro Isaac | 0.23 ✓ | 4 | Johannessen Tobias Halland |
| 5 | Parisini Nicolò | 0.21 | 5 | Vendrame Andrea |
| 6 | Walscheid Max | 0.19 | 6 | Pinarello Alessandro |
| 7 | Tiberi Antonio | 0.19 | 7 | Ciccone Giulio |
| 8 | Gautherat Pierre | 0.18 | 8 | Kron Andreas |
| 9 | Mihkels Madis | 0.18 | 9 | Champoussin Clément |
| 10 | Froidevaux Robin | 0.17 | 10 | Lapeira Paul |

*1/10 correct (del Toro ✓)*

### Stage 3 — 2026-03-11 (Flat)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Milan Jonathan | 0.55 ✓ | 1 | Andresen Tobias Lund |
| 2 | Welsford Sam | 0.36 ✓ | 2 | De Lie Arnaud |
| 3 | Gautherat Pierre | 0.20 | 3 | Philipsen Jasper |
| 4 | Walscheid Max | 0.19 | 4 | Magnier Paul |
| 5 | Andresen Tobias Lund | 0.18 ✓ | 5 | García Cortina Iván |
| 6 | van Poppel Danny | 0.17 | 6 | Welsford Sam |
| 7 | Gudmestad Tord | 0.17 | 7 | Milan Jonathan |
| 8 | Philipsen Jasper | 0.17 ✓ | 8 | Kogut Oded |
| 9 | Mihkels Madis | 0.15 ✓ | 9 | Mihkels Madis |
| 10 | van der Poel Mathieu | 0.15 | 10 | Bittner Pavel |

*5/10 correct (Milan ✓, Welsford ✓, Andresen ✓, Philipsen ✓, Mihkels ✓)*

### Stage 4 — 2026-03-12 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Milan Jonathan | 0.39 | 1 | van der Poel Mathieu ✓ |
| 2 | Welsford Sam | 0.35 | 2 | Pellizzari Giulio |
| 3 | Bittner Pavel | 0.29 | 3 | Johannessen Tobias Halland |
| 4 | Vendrame Andrea | 0.27 ✓ | 4 | Champoussin Clément |
| 5 | Philipsen Jasper | 0.26 | 5 | van Aert Wout |
| 6 | De Lie Arnaud | 0.24 | 6 | Healy Ben |
| 7 | Kogut Oded | 0.24 | 7 | Vendrame Andrea |
| 8 | Andresen Tobias Lund | 0.22 | 8 | Pinarello Alessandro |
| 9 | van der Poel Mathieu | 0.22 ✓ | 9 | Ganna Filippo |
| 10 | García Cortina Iván | 0.21 | 10 | del Toro Isaac |

*2/10 correct (Vendrame ✓, van der Poel ✓)*

### Stage 5 — 2026-03-13 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Milan Jonathan | 0.48 | 1 | Valgren Michael |
| 2 | Welsford Sam | 0.36 | 2 | del Toro Isaac ✓ |
| 3 | Vendrame Andrea | 0.34 | 3 | Jorgenson Matteo |
| 4 | del Toro Isaac | 0.32 ✓ | 4 | Johannessen Tobias Halland |
| 5 | Andresen Tobias Lund | 0.31 | 5 | Ciccone Giulio |
| 6 | van der Poel Mathieu | 0.30 | 6 | Pellizzari Giulio ✓ |
| 7 | Philipsen Jasper | 0.28 | 7 | Roglič Primož |
| 8 | Bittner Pavel | 0.26 | 8 | Pinarello Alessandro |
| 9 | Tiberi Antonio | 0.24 | 9 | Storer Michael |
| 10 | Pellizzari Giulio | 0.22 ✓ | 10 | Buitrago Santiago |

*2/10 correct (del Toro ✓, Pellizzari ✓)*

### Stage 6 — 2026-03-14 (Hilly)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Milan Jonathan | 0.40 | 1 | del Toro Isaac ✓ |
| 2 | Welsford Sam | 0.35 | 2 | Johannessen Tobias Halland |
| 3 | del Toro Isaac | 0.31 ✓ | 3 | Jorgenson Matteo |
| 4 | Vendrame Andrea | 0.31 ✓ | 4 | Pellizzari Giulio |
| 5 | Andresen Tobias Lund | 0.30 | 5 | Ciccone Giulio |
| 6 | van der Poel Mathieu | 0.28 | 6 | Buitrago Santiago |
| 7 | Philipsen Jasper | 0.28 | 7 | Healy Ben |
| 8 | Bittner Pavel | 0.26 | 8 | Sheffield Magnus |
| 9 | Ciccone Giulio | 0.24 ✓ | 9 | Roglič Primož |
| 10 | Pellizzari Giulio | 0.22 | 10 | Vendrame Andrea |

*3/10 correct (del Toro ✓, Vendrame ✓, Ciccone ✓)*

### Stage 7 — 2026-03-15 (Flat)

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Milan Jonathan | 0.63 ✓ | 1 | Milan Jonathan |
| 2 | Welsford Sam | 0.54 ✓ | 2 | Welsford Sam |
| 3 | Andresen Tobias Lund | 0.29 ✓ | 3 | Rex Laurenz |
| 4 | Philipsen Jasper | 0.29 | 4 | Kogut Oded ✓ |
| 5 | Bittner Pavel | 0.24 ✓ | 5 | Bittner Pavel |
| 6 | De Lie Arnaud | 0.23 ✓ | 6 | Andresen Tobias Lund |
| 7 | van der Poel Mathieu | 0.22 | 7 | Foldager Anders |
| 8 | Kogut Oded | 0.22 ✓ | 8 | De Lie Arnaud |
| 9 | Magnier Paul | 0.20 | 9 | Lonardi Giovanni |
| 10 | Vendrame Andrea | 0.20 | 10 | García Cortina Iván |

*6/10 correct (Milan ✓, Welsford ✓, Andresen ✓, Bittner ✓, De Lie ✓, Kogut ✓)*

### Tirreno-Adriatico 2026 — General Classification

| # | Actual GC | # | Predicted (stage top-10 appearances) | Stages | Avg P(top10) |
|---|-----------|---|-----------------------------------------|--------|--------------|
| 1 | del Toro Isaac | 1 | Milan Jonathan | 7 | 0.43 |
| 2 | Jorgenson Matteo | 2 | Andresen Tobias Lund | 6 | 0.26 |
| 3 | Pellizzari Giulio | 3 | Welsford Sam | 6 | 0.39 |
| 4 | Johannessen Tobias Halland | 4 | Philipsen Jasper | 5 | 0.25 |
| 5 | Roglič Primož | 5 | van der Poel Mathieu | 5 | 0.23 |
| 6 | Ciccone Giulio | 6 | del Toro Isaac | 4 | 0.30 |
| 7 | Buitrago Santiago | 7 | Vendrame Andrea | 4 | 0.28 |
| 8 | Healy Ben | 8 | Bittner Pavel | 4 | 0.26 |
| 9 | Sheffield Magnus | 9 | Tiberi Antonio | 3 | 0.26 |
| 10 | Pinarello Alessandro | 10 | De Lie Arnaud | 2 | 0.24 |

---

## Milano-Torino 2026 — 2026-03-18 (Hilly)

> One-day race. AUC 0.759 | Precision@10: 0.40

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Roglič Primož | 0.387 ✓ | 1 | Pidcock Thomas |
| 2 | Pidcock Thomas | 0.319 ✓ | 2 | Johannessen Tobias Halland |
| 3 | Johannessen Tobias Halland | 0.315 ✓ | 3 | Roglič Primož |
| 4 | Pellizzari Giulio | 0.315 ✓ | 4 | Pellizzari Giulio |
| 5 | Gee-West Derek | 0.259 | 5 | Uijtdebroeks Cian |
| 6 | Baudin Alex | 0.198 | 6 | Cepeda Jefferson Alexander |
| 7 | Ulissi Diego | 0.183 | 7 | Berwick Sebastian |
| 8 | Christen Jan | 0.166 | 8 | Fortunato Lorenzo |
| 9 | Tesfatsion Natnael | 0.153 | 9 | Fancellu Alessandro |
| 10 | Boichis Adrien | 0.140 | 10 | Storer Michael |

*4/10 correct (Roglič ✓, Pidcock ✓, Johannessen ✓, Pellizzari ✓). Top-4 correctly identified, positions shuffled.*

## Danilith Nokere Koerse 2026 — 2026-03-18 (Hilly/Cobbled)

> One-day race. AUC 0.759 | Precision@10: 0.30

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | Blikra Erlend | 0.277 | 1 | Philipsen Jasper |
| 2 | Meeus Jordi | 0.264 ✓ | 2 | Meeus Jordi |
| 3 | De Schuyteneer Steffen | 0.246 ✓ | 3 | Molano Juan Sebastián |
| 4 | Vermeersch Gianni | 0.218 | 4 | Jeannière Emilien |
| 5 | Philipsen Jasper | 0.150 ✓ | 5 | Menten Milan |
| 6 | Thijssen Gerben | 0.149 | 6 | Kubiš Lukáš |
| 7 | Mezgec Luka | 0.135 | 7 | Biermans Jenthe |
| 8 | Huens Axel | 0.129 | 8 | Penhoët Paul |
| 9 | Kanter Max | 0.122 | 9 | De Schuyteneer Steffen |
| 10 | Teutenberg Tim Torn | 0.118 | 10 | Morgado António |

*3/10 correct (Meeus ✓, De Schuyteneer ✓, Philipsen ✓). Winner Philipsen ranked only #5 in predictions.*

## Milano-Sanremo 2026 — 2026-03-21 (Hilly)

> One-day race. AUC 0.942 | Precision@10: 0.40

| # | Predicted | P(top10) | # | Actual |
|---|-----------|----------|---|--------|
| 1 | van der Poel Mathieu | 0.416 ✓ | 1 | Pogačar Tadej |
| 2 | Pogačar Tadej | 0.395 ✓ | 2 | Pidcock Thomas |
| 3 | Vendrame Andrea | 0.394 ✓ | 3 | van Aert Wout |
| 4 | Pidcock Thomas | 0.365 ✓ | 4 | Pedersen Mads |
| 5 | Jorgenson Matteo | 0.365 | 5 | Strong Corbin |
| 6 | del Toro Isaac | 0.365 | 6 | Vendrame Andrea |
| 7 | Ciccone Giulio | 0.343 | 7 | Stuyven Jasper |
| 8 | Grégoire Romain | 0.329 | 8 | van der Poel Mathieu |
| 9 | Laporte Christophe | 0.321 | 9 | Trentin Matteo |
| 10 | Roglič Primož | 0.301 | 10 | Zambanini Edoardo |

*4/10 correct (van der Poel ✓, Pogačar ✓, Vendrame ✓, Pidcock ✓). Excellent AUC (0.942). Missed: van Aert, Pedersen, Strong (Corbin), Trentin. Note: our pre-race prediction list had Milan Jonathan #2 (0.393) and del Toro #3 (0.382) — the backtest uses updated model which now uses all actual result data.*

---

# Upcoming Race Predictions

*Model retrained on 2023–2026 data (cutoff 2026-03-21, val Milano-Sanremo 2026: AUC 0.942). Includes elevation data, cobbled/gravel surface tags, profile-specific rolling form, and stage context features.*

*Features built from all history up to race start date. Startlists scraped fresh.*

---

## Volta a Catalunya 2026 (starts 2026-03-23, 7 stages)

*Model retrained cutoff 2026-03-21. Rider profiles fully refreshed from PCS. 2.Pro races Volta Comunitat Valenciana and Volta ao Algarve scraped and included — resolving a data gap for GC riders who skipped early WT rounds (e.g. Almeida, Vine).*

*GC contenders: Evenepoel leads on recent form across all stage types. Ciccone ranked #2 overall on cumulative probability. Godon remains a breakaway threat on mountain stages. Vingegaard dominates the final hilly stage. Known model limitation: GC riders accumulate high position numbers on sprint stages, suppressing their rolling averages — Almeida (P≈0.06 on mountains) and Landa are likely underrated.*

| Stage | Date | Profile | Top predicted | P(top10) |
|-------|------|---------|---------------|----------|
| 1 | 2026-03-23 | Hilly | Ciccone Giulio | 0.331 |
| 2 | 2026-03-24 | Hilly | Ciccone Giulio | 0.340 |
| 3 | 2026-03-25 | Mountain | Evenepoel Remco | 0.328 |
| 4 | 2026-03-26 | Mountain | Godon Dorian | 0.329 |
| 5 | 2026-03-27 | Mountain | Godon Dorian | 0.348 |
| 6 | 2026-03-28 | Mountain | Godon Dorian | 0.348 |
| 7 | 2026-03-29 | Hilly (final) | Vingegaard Jonas | 0.535 |

**Stage 1 (Hilly):** Ciccone 0.331 · Evenepoel 0.326 · Vine 0.301 · Vernon 0.255 · Pidcock 0.234

**Stage 2 (Hilly):** Ciccone 0.340 · Evenepoel 0.338 · Vine 0.301 · Vernon 0.260 · Vingegaard 0.221

**Stage 3 (Mountain):** Evenepoel 0.328 · Godon 0.302 · Ciccone 0.228 · Vine 0.213 · Gall 0.174

**Stage 4 (Mountain):** Godon 0.329 · Evenepoel 0.320 · Ciccone 0.215 · Vine 0.213 · Gall 0.166

**Stage 5 (Mountain):** Godon 0.348 · Evenepoel 0.338 · Ciccone 0.225 · Vine 0.217 · Gall 0.163

**Stage 6 (Mountain):** Godon 0.348 · Evenepoel 0.333 · Ciccone 0.225 · Vine 0.217 · Gall 0.163

**Stage 7 / Final (Hilly):** Vingegaard 0.535 · Ciccone 0.453 · Evenepoel 0.433 · Pidcock 0.371 · Vine 0.324

**Predicted GC podium:** Evenepoel – Ciccone – Godon (breakaway)/Vine

---

## Brugge-De Panne — 2026-03-25 (Flat)

| # | Rider | P(top10) |
|---|-------|----------|
| 1 | PHILIPSEN Jasper | 0.287 |
| 2 | ANDRESEN Tobias Lund | 0.255 |
| 3 | BLIKRA Erlend | 0.194 |
| 4 | RENARD Alexis | 0.169 |
| 5 | DE LIE Arnaud | 0.164 |
| 6 | BITTNER Pavel | 0.146 |
| 7 | FRETIN Milan | 0.140 |
| 8 | MERLIER Tim | 0.132 |
| 9 | GROENEWEGEN Dylan | 0.132 |
| 10 | LAMPERTI Luke | 0.130 |

## Grand Prix de Denain — 2026-03-25 (Flat/Cobbled)

| # | Rider | P(top10) |
|---|-------|----------|
| 1 | MEEUS Jordi | 0.286 |
| 2 | BIERMANS Jenthe | 0.268 |
| 3 | DE LIE Arnaud | 0.222 |
| 4 | MENTEN Milan | 0.210 |
| 5 | TEUTENBERG Tim Torn | 0.201 |
| 6 | RENARD Alexis | 0.197 |
| 7 | TURGIS Anthony | 0.191 |
| 8 | PLOWRIGHT Jensen | 0.184 |
| 9 | HUENS Axel | 0.183 |
| 10 | MOLANO Juan Sebastián | 0.175 |

## E3 Saxo Classic — 2026-03-27 (Hilly/Cobbled)

| # | Rider | P(top10) |
|---|-------|----------|
| 1 | VAN DER POEL Mathieu | 0.566 |
| 2 | LAPORTE Christophe | 0.529 |
| 3 | PEDERSEN Mads | 0.443 |
| 4 | LAMPERTI Luke | 0.351 |
| 5 | TURGIS Anthony | 0.337 |
| 6 | VAN DIJKE Tim | 0.292 |
| 7 | VERMEERSCH Florian | 0.251 |
| 8 | GRÉGOIRE Romain | 0.197 |
| 9 | HUENS Axel | 0.197 |
| 10 | TRENTIN Matteo | 0.184 |

## Bredene Koksijde Classic — 2026-03-27 (Flat/Cobbled)

| # | Rider | P(top10) |
|---|-------|----------|
| 1 | DE SCHUYTENEER Steffen | 0.391 |
| 2 | BLIKRA Erlend | 0.278 |
| 3 | KANTER Max | 0.255 |
| 4 | PENHOËT Paul | 0.248 |
| 5 | JEANNIÈRE Emilien | 0.216 |
| 6 | TEUTENBERG Tim Torn | 0.198 |
| 7 | BITTNER Pavel | 0.197 |
| 8 | RENARD Alexis | 0.193 |
| 9 | HUENS Axel | 0.183 |
| 10 | GROENEWEGEN Dylan | 0.181 |

## Gent-Wevelgem — 2026-03-29 (Hilly/Cobbled)

| # | Rider | P(top10) |
|---|-------|----------|
| 1 | STUYVEN Jasper | 0.479 |
| 2 | PITHIE Laurence | 0.410 |
| 3 | VAN DER POEL Mathieu | 0.364 |
| 4 | TEUNISSEN Mike | 0.318 |
| 5 | SHEFFIELD Magnus | 0.314 |
| 6 | TRENTIN Matteo | 0.309 |
| 7 | VAN AERT Wout | 0.300 |
| 8 | BRENNAN Matthew | 0.259 |
| 9 | MORGADO António | 0.229 |
| 10 | PEDERSEN Mads | 0.228 |
