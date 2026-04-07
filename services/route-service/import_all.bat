@echo off
echo Starting OSM imports sequentially...

echo [1/13] ireland (europe)
REM python import_osm.py --pbf pbfs\ireland-and-northern-ireland-260404.osm.pbf --region europe
echo ireland done.

echo [2/13] great-britain (europe)
python import_osm.py --pbf pbfs\great-britain-260404.osm.pbf --region europe
echo great-britain done.

echo [3/13] france (europe)
python import_osm.py --pbf pbfs\france-260404.osm.pbf --region europe
echo france done.

echo [4/13] germany (europe)
python import_osm.py --pbf pbfs\germany-latest.osm.pbf --region europe
echo germany done.

echo [5/13] austria (europe)
python import_osm.py --pbf pbfs\austria-260404.osm.pbf --region europe
echo austria done.

echo [6/13] hungary (europe)
python import_osm.py --pbf pbfs\hungary-260404.osm.pbf --region europe
echo hungary done.

echo [7/13] romania (europe)
python import_osm.py --pbf pbfs\romania-260404.osm.pbf --region europe
echo romania done.

echo [8/13] bulgaria (europe)
REM python import_osm.py --pbf pbfs\bulgaria-260404.osm.pbf --region europe
echo bulgaria done.

echo [9/13] turkey (middle-east)
REM python import_osm.py --pbf pbfs\turkey-260404.osm.pbf --region middle-east
echo turkey done.

echo [10/13] iran (middle-east)
python import_osm.py --pbf pbfs\iran-260404.osm.pbf --region middle-east
echo iran done.

echo [11/13] pakistan (middle-east)
python import_osm.py --pbf pbfs\pakistan-260404.osm.pbf --region middle-east
echo pakistan done.

echo [12/13] india (south-asia)
python import_osm.py --pbf pbfs\india-260404.osm.pbf --region south-asia
echo india done.

echo [13/13] texas (north-america)
REM python import_osm.py --pbf pbfs\texas-260404.osm.pbf --region north-america
echo texas done.

echo All imports complete.
