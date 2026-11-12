"""Синтетический VersionView: Python -> TypeScript -> Python, без HTTP-операций продукта."""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))
from app.contracts import PACKAGE, serialize_definition

view = json.loads((PACKAGE / "examples/entities.json").read_text())["records"]["answer_view"]["value"]
view['version']['basis']['sources']=[dict(source=p,original_filename='Синтетический.pdf',revision=1) for p in view['version']['provenance']['source_versions']]
view['version']['basis']['comparisons']=[]
args = sys.argv[1:]
if '--pdf-source' in args:
    args.remove('--pdf-source')
if '--json-source' in args:
    args.remove('--json-source')
    for fact in view['version']['basis']['facts']:
        for evidence in fact['evidence']:
            evidence['locator'] = dict(json_pointer='/normalized/price', area='Сохраненный раздел JSON')
    for source in view['version']['basis']['sources']:
        source['original_filename'] = 'Синтетический.json'
if not args:
    print(serialize_definition("VersionView", view))
else:
    actual = json.loads(Path(args[0]).read_text())
    assert json.loads(serialize_definition("VersionView", actual)) == view
    print("passed: Python -> TypeScript -> Python; Decimal strings, null and IDs unchanged")
