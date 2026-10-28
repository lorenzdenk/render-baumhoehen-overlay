# Daten

## Tasmanien: Baumgruppen ab 80 m

Aus theLIST **Tree Canopy Height** (LiDAR, 2 m, © State of Tasmania, CC BY 3.0 AU), nur Gebiete mit LiDAR.

- `tasmanien_gruppen_ab_80m.csv`: eine Zeile je Gruppe. Bäume ab 80 m, die höchstens 250 m voneinander entfernt
  stehen (Kette), bilden eine Gruppe. Spalten: höchster Baum (m), Lage des höchsten Baums, Anzahl Kronenspitzen
  ab 80 m bzw. ab 90 m, Ausdehnung (m).
- `tasmanien_gruppen_ab_80m.geojson`: alle Kronenspitzen ab 80 m mit Gruppennummer (höchster Baum je Gruppe markiert).
- `tasmanien_gruppen_ab_80m_verdaechtig.csv`: Spitzen über 102 m, höher als jeder bekannte Baum Tasmaniens
  (Centurion, 100,5 m), vermutlich Messfehler.

Herausgefiltert wurden vorher: Spitzen in OSM-Gebäuden, an Türmen, Masten, Windrädern und Leitungen
(© OpenStreetMap-Mitwirkende), sowie nach Form: Leitungsseile über Tälern (viele hohe Spitzen auf einer Geraden
oder langgestreckter Gipfel) und Einzelspitzen ohne Krone (mehr als 45 m über der Umgebung).

Eine Kronenspitze ist ein lokales Maximum im Umkreis von 5 m; grosse Eukalypten haben teils mehrere Spitzen,
die Anzahlen sind daher eher Kronenspitzen als Bäume. Höhen in ganzen Metern (so liefert sie theLIST).
