"""Build scripts/ledger.py from dev/core.py, dev/page.html and references/schema.sql."""
from pathlib import Path
root = Path(__file__).resolve().parent.parent
core = (root / "dev/core.py").read_text()
schema = (root / "references/schema.sql").read_text()
page = (root / "dev/page.html").read_text()
for name, text in (("schema", schema), ("page", page)):
    assert '"""' not in text, f"{name} must not contain triple quotes"
out = core.replace("@@SCHEMA@@", schema, 1).replace("@@PAGE@@", page, 1)
(root / "scripts/ledger.py").write_text(out)
print("scripts/ledger.py", len(out.splitlines()), "lines")
