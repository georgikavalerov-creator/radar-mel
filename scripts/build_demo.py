#!/usr/bin/env python3
"""Habitants et niveau de vie de la MEL, ramenés sur la grille H3 (résolution 8) du radar.

Source : INSEE, Filosofi 2019, données carroyées à 200 m (carreaux_200m_met.csv).
  https://www.insee.fr/fr/statistiques/7655475
Données annuelles : à relancer à la sortie d'un nouveau millésime.
  python scripts/build_demo.py chemin/vers/carreaux_200m_met.csv
Écrit demo.json : {cellule H3: [habitants, niveau de vie moyen en euros par an et par personne]}.
"""
import csv, json, re, sys
import h3
from pyproj import Transformer

BBOX = (50.50, 2.85, 50.80, 3.30)
RES = 8


def main(path, out='demo.json'):
    to_wgs = Transformer.from_crs('EPSG:3035', 'EPSG:4326', always_xy=True)
    agg = {}
    with open(path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if not row['lcog_geo'].startswith('59'):
                continue
            m = re.search(r'N(\d+)E(\d+)', row['idcar_200m'])
            if not m:
                continue
            lng, lat = to_wgs.transform(int(m.group(2)) + 100, int(m.group(1)) + 100)  # centre du carreau
            if not (BBOX[0] < lat < BBOX[2] and BBOX[1] < lng < BBOX[3]):
                continue
            cell = h3.latlng_to_cell(lat, lng, RES)
            a = agg.setdefault(cell, [0.0, 0.0])
            a[0] += float(row['ind'] or 0)
            a[1] += float(row['ind_snv'] or 0)  # somme des niveaux de vie des habitants du carreau
    pop = sum(a[0] for a in agg.values())
    cells = {c: [round(a[0]), round(a[1] / a[0]) if a[0] else 0] for c, a in agg.items() if a[0] >= 1}
    data = {'source': 'INSEE, Filosofi 2019, carreaux de 200 m', 'pop': round(pop),
            'inc': round(sum(a[1] for a in agg.values()) / pop), 'cells': cells}
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    print(len(cells), 'hexagones,', data['pop'], 'habitants, niveau de vie moyen', data['inc'], '€')


if __name__ == '__main__':
    main(*sys.argv[1:])
